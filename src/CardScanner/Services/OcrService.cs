using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using OpenCvSharp;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;

namespace CardScanner.Services;

/// <summary>
/// Reads the card's title (name) using the built-in Windows OCR engine — no native
/// Tesseract binaries required. Operates on the top "title strip" of a warped card.
/// </summary>
public sealed class OcrService
{
    private readonly OcrEngine? _engine;

    public bool Available => _engine != null;

    public OcrService()
    {
        // Prefer the user's languages; fall back to English if installed.
        _engine = OcrEngine.TryCreateFromUserProfileLanguages()
                  ?? TryEnglish();
    }

    private static OcrEngine? TryEnglish()
    {
        try
        {
            var lang = new Windows.Globalization.Language("en-US");
            return OcrEngine.IsLanguageSupported(lang) ? OcrEngine.TryCreateFromLanguage(lang) : null;
        }
        catch { return null; }
    }

    /// <summary>
    /// Crops the title strip from a warped 488x680 card and OCRs it. To cope with poor light
    /// and foil glare, several enhanced variants are tried and the best read is returned.
    /// </summary>
    public async Task<string> ReadTitleAsync(Mat warpedCard)
    {
        if (_engine == null || warpedCard.Empty()) return string.Empty;

        using var gray = CropTitleStripGray(warpedCard);

        // Build all variants up front (they derive from each other), then dispose them all.
        var variants = EnhanceVariants(gray);
        try
        {
            string best = "";
            foreach (var variant in variants)
            {
                using var swbmp = await ToSoftwareBitmapAsync(variant);
                if (swbmp == null) continue;
                OcrResult result = await _engine.RecognizeAsync(swbmp);
                string text = CleanName(result.Text);
                if (Score(text) > Score(best)) best = text;
                if (Score(best) >= 6) break; // good enough — stop early
            }
            return best;
        }
        finally
        {
            foreach (var v in variants) v.Dispose();
        }
    }

    /// <summary>Letters in the read — a rough "how much real text did we get" score.</summary>
    private static int Score(string s) => s.Count(char.IsLetter);

    private static Mat CropTitleStripGray(Mat card)
    {
        // Title band sits near the top; name text is on the left, mana cost on the right.
        int w = card.Width, h = card.Height;
        int x0 = (int)(w * 0.03);
        int x1 = (int)(w * 0.78);
        int y0 = (int)(h * 0.032);
        int y1 = (int)(h * 0.095);
        var rect = new Rect(x0, y0, x1 - x0, y1 - y0).Intersect(new Rect(0, 0, w, h));

        using var crop = new Mat(card, rect);
        using var scaled = new Mat();
        Cv2.Resize(crop, scaled, new Size(rect.Width * 3, rect.Height * 3), 0, 0, InterpolationFlags.Cubic);
        var gray = new Mat();
        Cv2.CvtColor(scaled, gray, ColorConversionCodes.BGR2GRAY);
        return gray;
    }

    /// <summary>
    /// Build progressively-processed versions of the title strip: CLAHE-equalized (boosts
    /// contrast in dim light), an Otsu threshold (dark text on light), and an inverted Otsu
    /// threshold (light text on dark title bars). Computed eagerly — the threshold variants
    /// derive from the equalized image, so nothing is used after it could be disposed.
    /// The caller owns and disposes every returned Mat.
    /// </summary>
    private static List<Mat> EnhanceVariants(Mat gray)
    {
        var eq = new Mat();
        using (var clahe = Cv2.CreateCLAHE(clipLimit: 2.5, tileGridSize: new Size(8, 8)))
            clahe.Apply(gray, eq);

        var th = new Mat();
        Cv2.Threshold(eq, th, 0, 255, ThresholdTypes.Binary | ThresholdTypes.Otsu);

        var inv = new Mat();
        Cv2.Threshold(eq, inv, 0, 255, ThresholdTypes.BinaryInv | ThresholdTypes.Otsu);

        return new List<Mat> { eq, th, inv };
    }

    private static async Task<SoftwareBitmap?> ToSoftwareBitmapAsync(Mat mat)
    {
        if (!Cv2.ImEncode(".png", mat, out byte[] png)) return null;
        using var ms = new MemoryStream(png);
        using var ras = ms.AsRandomAccessStream();
        var decoder = await BitmapDecoder.CreateAsync(ras);
        var swbmp = await decoder.GetSoftwareBitmapAsync(
            BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied);
        return swbmp;
    }

    /// <summary>Normalize OCR output into a plausible card name.</summary>
    private static string CleanName(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return string.Empty;
        // Take the first line; card names are single-line titles.
        var firstLine = raw.Replace("\r", " ").Split('\n')[0].Trim();
        var sb = new System.Text.StringBuilder();
        foreach (char c in firstLine)
        {
            if (char.IsLetter(c) || char.IsWhiteSpace(c) || c is '\'' or '-' or ',' or '.' or '/')
                sb.Append(c);
        }
        return System.Text.RegularExpressions.Regex.Replace(sb.ToString(), @"\s+", " ").Trim();
    }
}

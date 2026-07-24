using System.IO;
using System.Text;
using System.Threading.Tasks;
using CardScanner.Models;
using CardScanner.Services;
using OpenCvSharp;

namespace CardScanner;

/// <summary>
/// Headless verification of the non-UI pipeline. Writes a report to a log file
/// (second CLI arg, or %TEMP%\cardscanner_selftest.log) and returns 0 on success.
/// Network-dependent checks (Scryfall) are reported as SKIP when offline, not failures.
/// </summary>
public static class SelfTest
{
    public static async Task<int> RunAsync(string[] args)
    {
        string logPath = args.Length > 1 && !args[1].StartsWith("--")
            ? args[1]
            : Path.Combine(Path.GetTempPath(), "cardscanner_selftest.log");

        var log = new StringBuilder();
        int failures = 0;
        void Pass(string name, string? extra = null) => log.AppendLine($"PASS  {name}{(extra != null ? "  — " + extra : "")}");
        void Skip(string name, string why) => log.AppendLine($"SKIP  {name}  — {why}");
        void Fail(string name, string why) { failures++; log.AppendLine($"FAIL  {name}  — {why}"); }

        log.AppendLine("=== CardScanner self-test ===");

        // 1. Database round-trip
        try
        {
            string dbPath = Path.Combine(Path.GetTempPath(), $"cardscanner_test_{Guid.NewGuid():N}.db");
            var db = new Database(dbPath);
            var card = new ScannedCard { ScryfallId = "test-id-1", Name = "Test Card", SetCode = "tst", PriceUsd = 1.23m };
            db.AddOrIncrement(card);
            db.AddOrIncrement(card); // should increment to qty 2
            var all = db.GetCollection();
            if (all.Count == 1 && all[0].Quantity == 2 && all[0].Name == "Test Card")
                Pass("Database round-trip", $"qty={all[0].Quantity}");
            else
                Fail("Database round-trip", $"unexpected state: count={all.Count}");
            try { File.Delete(dbPath); } catch { }
        }
        catch (Exception ex) { Fail("Database round-trip", ex.Message); }

        // 2. Perceptual hashing
        try
        {
            var hasher = new PerceptualHasher();
            using var a = new Mat(200, 140, MatType.CV_8UC3, Scalar.All(255));
            Cv2.Rectangle(a, new Rect(20, 20, 100, 60), new Scalar(0, 0, 0), -1);
            using var b = a.Clone();
            using var c = new Mat(200, 140, MatType.CV_8UC3, Scalar.All(255));
            Cv2.Circle(c, new Point(70, 100), 50, new Scalar(0, 0, 0), -1);

            Cv2.ImEncode(".png", a, out byte[] ba);
            Cv2.ImEncode(".png", b, out byte[] bb);
            Cv2.ImEncode(".png", c, out byte[] bc);
            ulong ha = hasher.HashFromEncoded(ba), hb = hasher.HashFromEncoded(bb), hc = hasher.HashFromEncoded(bc);

            int same = PerceptualHasher.HammingDistance(ha, hb);
            int diff = PerceptualHasher.HammingDistance(ha, hc);
            if (same == 0 && diff > same)
                Pass("Perceptual hashing", $"identical dist={same}, different dist={diff}");
            else
                Fail("Perceptual hashing", $"identical dist={same}, different dist={diff}");

            // Art-crop hash: distinct from the full hash, stable for identical images.
            var (fullA, artA) = hasher.HashFullAndArt(ba);
            var (fullB, artB) = hasher.HashFullAndArt(bb);
            var (_, artC) = hasher.HashFullAndArt(bc);
            int artSame = PerceptualHasher.HammingDistance(artA, artB);
            int artDiff = PerceptualHasher.HammingDistance(artA, artC);
            if (fullA == ha && artA != 0 && artSame == 0 && artDiff > artSame)
                Pass("Art-crop hash", $"art identical dist={artSame}, different dist={artDiff}");
            else
                Fail("Art-crop hash", $"fullMatch={fullA == ha}, artA={artA}, same={artSame}, diff={artDiff}");
        }
        catch (Exception ex) { Fail("Perceptual hashing", ex.Message); }

        // 3. Card detection + perspective warp
        try
        {
            using var frame = new Mat(700, 900, MatType.CV_8UC3, Scalar.All(20));
            // A card-shaped quad (~0.716 ratio) with slight perspective.
            var quad = new[]
            {
                new Point(300, 130), new Point(600, 150),
                new Point(590, 560), new Point(310, 540)
            };
            Cv2.FillConvexPoly(frame, quad, Scalar.All(235));
            // Add an inner border so edges are crisp.
            Cv2.Polylines(frame, new[] { quad }, true, Scalar.All(120), 3);

            var detector = new CardDetector();
            using var det = detector.Detect(frame);
            if (det.Found && det.Warped is { Width: CardDetector.CardWidth, Height: CardDetector.CardHeight })
                Pass("Card detection + warp", $"areaFraction={det.AreaFraction:0.00}");
            else
                Fail("Card detection + warp", $"found={det.Found}, warped={(det.Warped == null ? "null" : $"{det.Warped.Width}x{det.Warped.Height}")}");
        }
        catch (Exception ex) { Fail("Card detection + warp", ex.Message); }

        // 4. Windows OCR on a rendered title strip
        string ocrRead = "";
        try
        {
            var ocr = new OcrService();
            if (!ocr.Available)
            {
                Skip("Windows OCR", "no OCR language pack available");
            }
            else
            {
                using var card = new Mat(CardDetector.CardHeight, CardDetector.CardWidth, MatType.CV_8UC3, Scalar.All(240));
                Cv2.PutText(card, "Lightning Bolt", new Point(20, 52),
                    HersheyFonts.HersheyDuplex, 0.9, new Scalar(10, 10, 10), 2, LineTypes.AntiAlias);
                ocrRead = await ocr.ReadTitleAsync(card);

                // Regression guard: a blank strip yields no early match, forcing every
                // enhancement variant to be built and disposed (this previously threw a
                // "disposed Mat" from a use-after-yield in the variant iterator).
                using var blank = new Mat(CardDetector.CardHeight, CardDetector.CardWidth, MatType.CV_8UC3, Scalar.All(90));
                string blankRead = await ocr.ReadTitleAsync(blank);

                if (!string.IsNullOrWhiteSpace(ocrRead) && ocrRead.Any(char.IsLetter))
                    Pass("Windows OCR", $"read=\"{ocrRead}\" (blank-strip path ok)");
                else
                    Fail("Windows OCR", $"read nothing usable (\"{ocrRead}\")");
            }
        }
        catch (Exception ex) { Fail("Windows OCR", ex.Message); }

        // 5. Scryfall fuzzy lookup (network)
        ScannedCard? online = null;
        using var netCts = new System.Threading.CancellationTokenSource(TimeSpan.FromSeconds(25));
        try
        {
            using var scry = new ScryfallClient();
            online = await scry.GetByFuzzyNameAsync("Llanowar Elves", netCts.Token);
            if (online != null && online.Name.Contains("Llanowar", StringComparison.OrdinalIgnoreCase))
                Pass("Scryfall fuzzy lookup", $"{online.Name} [{online.SetCode}] ${online.PriceUsd}");
            else if (online == null)
                Skip("Scryfall fuzzy lookup", "no result (offline or API unreachable)");
            else
                Fail("Scryfall fuzzy lookup", $"unexpected: {online.Name}");
        }
        catch (Exception ex) { Skip("Scryfall fuzzy lookup", "network error: " + ex.Message); }

        // 6. End-to-end matcher (uses OCR text from #4 + Scryfall)
        try
        {
            if (online == null)
            {
                Skip("Hybrid matcher", "requires Scryfall network access");
            }
            else
            {
                string dbPath = Path.Combine(Path.GetTempPath(), $"cardscanner_match_{Guid.NewGuid():N}.db");
                var db = new Database(dbPath);
                using var scry = new ScryfallClient();
                var matcher = new CardMatcher(db, scry, new PerceptualHasher());
                using var card = new Mat(CardDetector.CardHeight, CardDetector.CardWidth, MatType.CV_8UC3, Scalar.All(240));
                var res = await matcher.IdentifyAsync(card, "Llanowar Elves", netCts.Token);
                if (res.Success && res.Card!.Name.Contains("Llanowar", StringComparison.OrdinalIgnoreCase))
                    Pass("Hybrid matcher", $"{res.Method} conf={res.Confidence:0.00} -> {res.Card.Name}");
                else
                    Fail("Hybrid matcher", $"success={res.Success} notes={res.Notes}");
                try { File.Delete(dbPath); } catch { }
            }
        }
        catch (Exception ex) { Fail("Hybrid matcher", ex.Message); }

        // 7. Finish/condition dedup in the collection
        try
        {
            string dbPath = Path.Combine(Path.GetTempPath(), $"cardscanner_dedup_{Guid.NewGuid():N}.db");
            var db = new Database(dbPath);
            ScannedCard NonFoil() => new() { ScryfallId = "sid-x", Name = "Sol Ring", SetCode = "cmr", CollectorNumber = "472", Foil = false, Condition = "NM", Language = "en" };
            ScannedCard Foil() => new() { ScryfallId = "sid-x", Name = "Sol Ring", SetCode = "cmr", CollectorNumber = "472", Foil = true, Condition = "NM", Language = "en" };
            db.AddOrIncrement(NonFoil());
            db.AddOrIncrement(NonFoil()); // -> non-foil qty 2
            db.AddOrIncrement(Foil());    // -> separate foil row qty 1
            var rows = db.GetCollection();
            var nf = rows.FirstOrDefault(r => !r.Foil);
            var f = rows.FirstOrDefault(r => r.Foil);
            if (rows.Count == 2 && nf?.Quantity == 2 && f?.Quantity == 1)
                Pass("Finish-based dedup", "non-foil x2 + foil x1 as separate rows");
            else
                Fail("Finish-based dedup", $"rows={rows.Count}, nf={nf?.Quantity}, f={f?.Quantity}");
            try { File.Delete(dbPath); } catch { }
        }
        catch (Exception ex) { Fail("Finish-based dedup", ex.Message); }

        // 8. Export format correctness
        try
        {
            var cards = new[]
            {
                new ScannedCard { ScryfallId = "abc-123", Name = "Lightning Bolt", SetCode = "2x2",
                    CollectorNumber = "117", Foil = true, Condition = "LP", Language = "ja",
                    Quantity = 2, PriceUsd = 1.00m, PriceUsdFoil = 3.00m }
            };

            string mox = CollectionExporter.Export(cards, ExportFormat.MoxfieldText);
            string moxCsv = CollectionExporter.Export(cards, ExportFormat.MoxfieldCsv);
            string arch = CollectionExporter.Export(cards, ExportFormat.ArchidektCsv);
            string plain = CollectionExporter.Export(cards, ExportFormat.PlainTextList);

            bool okText = mox.Contains("2 Lightning Bolt (2X2) 117 *F*");
            bool okMox = moxCsv.Contains("Count,Name,Edition,Condition,Language,Foil")
                         && moxCsv.Contains("Lightly Played") && moxCsv.Contains("Japanese")
                         && moxCsv.Contains(",foil,") && moxCsv.Contains("2x2");
            bool okArch = arch.Contains("Scryfall ID") && arch.Contains("abc-123") && arch.Contains("Foil");
            bool okPlain = plain.Trim() == "2 Lightning Bolt";

            if (okText && okMox && okArch && okPlain)
                Pass("Export formats", "Moxfield text/CSV, Archidekt CSV, plain list");
            else
                Fail("Export formats", $"text={okText}, moxCsv={okMox}, arch={okArch}, plain={okPlain}");
        }
        catch (Exception ex) { Fail("Export formats", ex.Message); }

        // 9. Scryfall manual search + printings (network)
        try
        {
            if (online == null)
            {
                Skip("Scryfall search/printings", "requires network access");
            }
            else
            {
                using var scry = new ScryfallClient();
                var search = await scry.SearchAsync("Llanowar Elves", netCts.Token);
                var prints = await scry.GetPrintingsAsync("Llanowar Elves", netCts.Token);
                bool okSearch = search.Any(c => c.Name.Contains("Llanowar", StringComparison.OrdinalIgnoreCase));
                bool okPrints = prints.Count > 1; // this card has many printings
                if (okSearch && okPrints)
                    Pass("Scryfall search/printings", $"search={search.Count}, printings={prints.Count}");
                else
                    Fail("Scryfall search/printings", $"search={search.Count}(match={okSearch}), printings={prints.Count}");
            }
        }
        catch (Exception ex) { Skip("Scryfall search/printings", "network error: " + ex.Message); }

        // 10. Video device enumeration by name
        try
        {
            var devices = CameraService.EnumerateDevices();
            if (devices.Count == 0)
                Skip("Video device names", "no video devices present");
            else if (devices.All(d => !string.IsNullOrWhiteSpace(d.Name)))
                Pass("Video device names", string.Join(", ", devices.Select(d => $"[{d.Index}] {d.Name}")));
            else
                Fail("Video device names", "some devices returned empty names");
        }
        catch (Exception ex) { Fail("Video device names", ex.Message); }

        log.AppendLine($"=== {(failures == 0 ? "ALL CORE TESTS PASSED" : $"{failures} FAILURE(S)")} ===");

        try { File.WriteAllText(logPath, log.ToString()); } catch { }
        TryWriteConsole(log.ToString());
        return failures == 0 ? 0 : 1;
    }

    // WPF apps have no console; attach to the parent's so output is visible when launched from a shell.
    private static void TryWriteConsole(string text)
    {
        try
        {
            AttachConsole(-1);
            Console.Out.Write(text);
            Console.Out.Flush();
        }
        catch { /* no console to attach */ }
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern bool AttachConsole(int dwProcessId);
}

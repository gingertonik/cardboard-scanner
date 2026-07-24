using OpenCvSharp;

namespace CardScanner.Services;

/// <summary>
/// Result of locating a card within a video frame.
/// </summary>
public sealed class DetectionResult : IDisposable
{
    /// <summary>True if a card-like quadrilateral was found.</summary>
    public bool Found { get; init; }
    /// <summary>The four corners of the detected card in the original frame (for overlay drawing).</summary>
    public Point[]? Quad { get; init; }
    /// <summary>Perspective-corrected, upright card image (BGR), or null.</summary>
    public Mat? Warped { get; init; }
    /// <summary>Fraction of the frame area occupied by the card (a rough "how close/filling" metric).</summary>
    public double AreaFraction { get; init; }

    public void Dispose() => Warped?.Dispose();
}

/// <summary>
/// Finds a Magic card in a frame using contour detection, then perspective-warps it upright.
/// MTG cards are 63 x 88 mm (aspect ratio ~0.716). We warp to a fixed 488 x 680 canvas.
/// </summary>
public sealed class CardDetector
{
    public const int CardWidth = 488;
    public const int CardHeight = 680;

    /// <summary>Minimum fraction of the frame the card must fill to be considered.</summary>
    public double MinAreaFraction { get; set; } = 0.06;

    public DetectionResult Detect(Mat frameBgr)
    {
        if (frameBgr.Empty())
            return new DetectionResult { Found = false };

        double frameArea = frameBgr.Width * (double)frameBgr.Height;

        using var gray = new Mat();
        Cv2.CvtColor(frameBgr, gray, ColorConversionCodes.BGR2GRAY);
        using var blurred = new Mat();
        Cv2.GaussianBlur(gray, blurred, new Size(5, 5), 0);
        using var edges = new Mat();
        Cv2.Canny(blurred, edges, 50, 150);
        // Close small gaps so the card outline is a continuous contour.
        using var kernel = Cv2.GetStructuringElement(MorphShapes.Rect, new Size(5, 5));
        Cv2.Dilate(edges, edges, kernel);

        Cv2.FindContours(edges, out Point[][] contours, out _,
            RetrievalModes.External, ContourApproximationModes.ApproxSimple);

        Point[]? bestQuad = null;
        double bestArea = 0;

        foreach (var contour in contours)
        {
            double area = Cv2.ContourArea(contour);
            if (area < frameArea * MinAreaFraction) continue;

            double peri = Cv2.ArcLength(contour, true);
            Point[] approx = Cv2.ApproxPolyDP(contour, 0.02 * peri, true);
            if (approx.Length != 4) continue;
            if (!Cv2.IsContourConvex(approx)) continue;

            // Reject shapes whose aspect ratio is nowhere near a card.
            if (!AspectLooksLikeCard(approx)) continue;

            if (area > bestArea)
            {
                bestArea = area;
                bestQuad = approx;
            }
        }

        if (bestQuad == null)
            return new DetectionResult { Found = false };

        var ordered = OrderCorners(bestQuad);
        Mat warped = WarpToCard(frameBgr, ordered);

        return new DetectionResult
        {
            Found = true,
            Quad = bestQuad,
            Warped = warped,
            AreaFraction = bestArea / frameArea
        };
    }

    private static bool AspectLooksLikeCard(Point[] quad)
    {
        var o = OrderCorners(quad);
        double widthTop = Distance(o[0], o[1]);
        double widthBottom = Distance(o[3], o[2]);
        double heightLeft = Distance(o[0], o[3]);
        double heightRight = Distance(o[1], o[2]);
        double w = (widthTop + widthBottom) / 2.0;
        double h = (heightLeft + heightRight) / 2.0;
        if (w < 1 || h < 1) return false;
        // Use the long/short ratio so the card can be held in portrait or landscape.
        double longSide = System.Math.Max(w, h);
        double shortSide = System.Math.Min(w, h);
        double ratio = shortSide / longSide;   // card ~0.716
        return ratio is > 0.55 and < 0.85;
    }

    /// <summary>Order corners as [top-left, top-right, bottom-right, bottom-left].</summary>
    private static Point[] OrderCorners(Point[] pts)
    {
        // Top-left has the smallest x+y, bottom-right the largest.
        // Top-right has the smallest y-x, bottom-left the largest.
        Point tl = pts[0], br = pts[0], tr = pts[0], bl = pts[0];
        int minSum = int.MaxValue, maxSum = int.MinValue, minDiff = int.MaxValue, maxDiff = int.MinValue;
        foreach (var p in pts)
        {
            int sum = p.X + p.Y;
            int diff = p.Y - p.X;
            if (sum < minSum) { minSum = sum; tl = p; }
            if (sum > maxSum) { maxSum = sum; br = p; }
            if (diff < minDiff) { minDiff = diff; tr = p; }
            if (diff > maxDiff) { maxDiff = diff; bl = p; }
        }
        return new[] { tl, tr, br, bl };
    }

    private static Mat WarpToCard(Mat src, Point[] ordered)
    {
        // If the detected card is wider than tall, it was held sideways: warp to a
        // landscape canvas then rotate upright so OCR/hashing see a portrait card.
        double w = (Distance(ordered[0], ordered[1]) + Distance(ordered[3], ordered[2])) / 2.0;
        double h = (Distance(ordered[0], ordered[3]) + Distance(ordered[1], ordered[2])) / 2.0;
        bool landscape = w > h;

        int dstW = landscape ? CardHeight : CardWidth;
        int dstH = landscape ? CardWidth : CardHeight;

        var srcPts = new Point2f[]
        {
            new(ordered[0].X, ordered[0].Y),
            new(ordered[1].X, ordered[1].Y),
            new(ordered[2].X, ordered[2].Y),
            new(ordered[3].X, ordered[3].Y),
        };
        var dstPts = new Point2f[]
        {
            new(0, 0),
            new(dstW - 1, 0),
            new(dstW - 1, dstH - 1),
            new(0, dstH - 1),
        };

        using var transform = Cv2.GetPerspectiveTransform(srcPts, dstPts);
        var warped = new Mat();
        Cv2.WarpPerspective(src, warped, transform, new Size(dstW, dstH));

        if (landscape)
        {
            var rotated = new Mat();
            Cv2.Rotate(warped, rotated, RotateFlags.Rotate90Clockwise);
            warped.Dispose();
            return rotated;
        }
        return warped;
    }

    private static double Distance(Point a, Point b)
    {
        double dx = a.X - b.X, dy = a.Y - b.Y;
        return System.Math.Sqrt(dx * dx + dy * dy);
    }
}

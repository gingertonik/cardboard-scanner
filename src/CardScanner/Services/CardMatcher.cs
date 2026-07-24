using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using CardScanner.Models;
using OpenCvSharp;

namespace CardScanner.Services;

/// <summary>
/// Hybrid card identification:
///   1. OCR the title, look it up by fuzzy name on Scryfall.
///   2. Perceptual-hash the card against the local Scryfall index (fallback / confirmation).
/// The two signals are combined: a name lookup that the image hash also agrees with is
/// reported as <see cref="MatchMethod.HybridConfirmed"/> with high confidence.
/// </summary>
public sealed class CardMatcher
{
    private readonly Database _db;
    private readonly ScryfallClient _scryfall;
    private readonly PerceptualHasher _hasher;

    // In-memory copy of the index for fast Hamming search.
    private List<CardIndexEntry> _index = new();

    /// <summary>Max Hamming distance (of 64 bits) to accept a pure image-hash match.</summary>
    /// <summary>Max Hamming distance to accept a *standalone* image-hash match.</summary>
    public int PhashAcceptDistance { get; set; } = 8;
    /// <summary>
    /// The best match must beat the runner-up by at least this many bits. Without a clear
    /// margin the match is ambiguous (common when the true card isn't in the index), so we
    /// reject rather than confidently return the nearest neighbour.
    /// </summary>
    public int PhashMarginRequirement { get; set; } = 4;
    /// <summary>Max Hamming distance for the image hash to "confirm" an OCR name hit.</summary>
    public int PhashConfirmDistance { get; set; } = 16;

    public int IndexSize => _index.Count;

    public CardMatcher(Database db, ScryfallClient scryfall, PerceptualHasher hasher)
    {
        _db = db;
        _scryfall = scryfall;
        _hasher = hasher;
    }

    public void ReloadIndex() => _index = _db.LoadIndex();

    /// <summary>Compute the whole-card and art-crop perceptual hashes of a warped card.</summary>
    public (ulong Full, ulong Art) HashCard(Mat warpedCard)
    {
        if (!Cv2.ImEncode(".png", warpedCard, out byte[] png))
            return (0, 0);
        return _hasher.HashFullAndArt(png);
    }

    public async Task<MatchResult> IdentifyAsync(Mat warpedCard, string ocrName, CancellationToken ct = default)
    {
        var (liveFull, liveArt) = HashCard(warpedCard);

        // Best local image-hash candidate (if we have an index), with the runner-up distance.
        // Each entry is scored by the better of its whole-card and art-crop hash distances,
        // so a foil whose whole-card hash is wrecked by glare can still match on its art.
        (CardIndexEntry entry, int dist, int secondDist, bool viaArt)? best = FindBestByHash(liveFull, liveArt);

        // --- Path A: OCR name -> Scryfall fuzzy lookup ---
        if (!string.IsNullOrWhiteSpace(ocrName) && ocrName.Length >= 3)
        {
            ScannedCard? named = null;
            try { named = await _scryfall.GetByFuzzyNameAsync(ocrName, ct); }
            catch { /* offline / transient: fall through to hash path */ }

            if (named != null)
            {
                double nameSim = StringSimilarity(Normalize(ocrName), Normalize(named.Name));

                // Does the image hash agree with the named card's printing?
                bool hashConfirms = best is { } b
                    && b.dist <= PhashConfirmDistance
                    && string.Equals(Normalize(b.entry.Name), Normalize(named.Name), StringComparison.Ordinal);

                if (hashConfirms)
                {
                    return new MatchResult
                    {
                        Success = true,
                        Method = MatchMethod.HybridConfirmed,
                        Confidence = Math.Min(0.99, 0.80 + nameSim * 0.19),
                        Card = named,
                        OcrText = ocrName,
                        Notes = "OCR name confirmed by image hash."
                    };
                }

                // Name lookup only — confidence driven by OCR string similarity.
                if (nameSim >= 0.55)
                {
                    return new MatchResult
                    {
                        Success = true,
                        Method = MatchMethod.OcrNameLookup,
                        Confidence = Math.Min(0.95, 0.45 + nameSim * 0.5),
                        Card = named,
                        OcrText = ocrName,
                        Notes = "Matched by card name (OCR)."
                    };
                }
            }
        }

        // --- Path B: pure perceptual-hash fallback ---
        // Accept only a close match that clearly beats the runner-up. This rejects the
        // "nearest neighbour of an incomplete index" case that otherwise yields a confident
        // but wrong result (e.g. matching a card that was never indexed).
        if (best is { } hb)
        {
            int margin = hb.secondDist - hb.dist;
            bool strong = hb.dist <= PhashAcceptDistance && margin >= PhashMarginRequirement;
            if (strong)
            {
                ScannedCard card;
                try { card = await _scryfall.GetByIdAsync(hb.entry.ScryfallId, ct) ?? FromIndex(hb.entry); }
                catch { card = FromIndex(hb.entry); }

                // Confidence from distance (0 bits -> ~0.95, accept-limit -> ~0.60).
                double conf = 0.60 + 0.35 * (PhashAcceptDistance - hb.dist) / (double)PhashAcceptDistance;
                return new MatchResult
                {
                    Success = true,
                    Method = MatchMethod.PerceptualHash,
                    Confidence = Math.Clamp(conf, 0.60, 0.95),
                    Card = card,
                    OcrText = ocrName,
                    Notes = $"Image-hash match via {(hb.viaArt ? "art crop" : "whole card")} (distance {hb.dist}, margin {margin})."
                };
            }
        }

        // Nothing confident. Explain the most likely reason so the user can act on it.
        string why = _index.Count == 0
            ? "No confident match — OCR couldn't read the name and the image index is empty. Build the index and/or improve lighting."
            : best is { } b2 && b2.dist <= PhashAcceptDistance
                ? $"No confident match — closest image (dist {b2.dist}) is ambiguous (margin {b2.secondDist - b2.dist}). The exact card may not be indexed yet."
                : $"No confident match — nearest indexed image is too different (dist {(best?.dist.ToString() ?? "n/a")}). Try better lighting, or add it via Manual search.";
        return MatchResult.Fail(ocrName, why);
    }

    private (CardIndexEntry entry, int dist, int secondDist, bool viaArt)? FindBestByHash(ulong liveFull, ulong liveArt)
    {
        if (_index.Count == 0) return null;
        CardIndexEntry? bestEntry = null;
        int bestDist = int.MaxValue, secondDist = int.MaxValue;
        bool bestViaArt = false;
        foreach (var e in _index)
        {
            int dFull = PerceptualHasher.HammingDistance(liveFull, e.PerceptualHash);
            // Art hash of 0 means "not computed" — ignore it rather than treat as a match.
            int dArt = e.ArtHash == 0 ? 64 : PerceptualHasher.HammingDistance(liveArt, e.ArtHash);
            bool viaArt = dArt < dFull;
            int d = viaArt ? dArt : dFull;

            if (d < bestDist)
            {
                secondDist = bestDist;
                bestDist = d;
                bestEntry = e;
                bestViaArt = viaArt;
            }
            else if (d < secondDist)
            {
                secondDist = d;
            }
        }
        return bestEntry == null ? null : (bestEntry, bestDist, secondDist, bestViaArt);
    }

    private static ScannedCard FromIndex(CardIndexEntry e) => new()
    {
        ScryfallId = e.ScryfallId,
        Name = e.Name,
        SetCode = e.SetCode,
        CollectorNumber = e.CollectorNumber,
        ImageUri = e.ImageUri,
        ScannedAt = DateTimeOffset.Now
    };

    // ---- text helpers ----

    private static string Normalize(string s)
        => new string(s.ToLowerInvariant().Where(c => char.IsLetterOrDigit(c) || c == ' ').ToArray()).Trim();

    /// <summary>Normalized Levenshtein similarity in [0,1].</summary>
    public static double StringSimilarity(string a, string b)
    {
        if (a.Length == 0 && b.Length == 0) return 1;
        if (a.Length == 0 || b.Length == 0) return 0;
        int[] prev = new int[b.Length + 1];
        int[] curr = new int[b.Length + 1];
        for (int j = 0; j <= b.Length; j++) prev[j] = j;
        for (int i = 1; i <= a.Length; i++)
        {
            curr[0] = i;
            for (int j = 1; j <= b.Length; j++)
            {
                int cost = a[i - 1] == b[j - 1] ? 0 : 1;
                curr[j] = Math.Min(Math.Min(curr[j - 1] + 1, prev[j] + 1), prev[j - 1] + cost);
            }
            (prev, curr) = (curr, prev);
        }
        int dist = prev[b.Length];
        int max = Math.Max(a.Length, b.Length);
        return 1.0 - (double)dist / max;
    }
}

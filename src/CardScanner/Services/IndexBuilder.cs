using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using CardScanner.Models;

namespace CardScanner.Services;

public sealed record IndexProgress(int Processed, int Added, int Skipped, string? CurrentName, bool Done, string? Message = null);

/// <summary>
/// Builds/updates the local perceptual-hash index from Scryfall bulk data.
///
/// The bulk JSON (hundreds of MB) is downloaded to a temp file *first*, then parsed from
/// disk while card images are downloaded and hashed with bounded concurrency. Downloading
/// up front avoids holding one giant HTTP response open for the entire (long) hashing pass,
/// which previously caused the transfer to be cut off after a few thousand cards.
/// Resumable: cards already in the index are skipped, and an intact temp file is reused.
/// </summary>
public sealed class IndexBuilder
{
    private readonly Database _db;
    private readonly ScryfallClient _scryfall;
    private readonly PerceptualHasher _hasher;

    /// <summary>Concurrent image downloads (served from Scryfall's CDN).</summary>
    public int ImageConcurrency { get; set; } = 6;

    /// <summary>Cards processed per batch (download+hash, then one DB write).</summary>
    public int BatchSize { get; set; } = 200;

    public IndexBuilder(Database db, ScryfallClient scryfall, PerceptualHasher hasher)
    {
        _db = db;
        _scryfall = scryfall;
        _hasher = hasher;
    }

    private static int ApproxCount(string bulkType) => bulkType switch
    {
        "unique_artwork" => 55_000,
        "default_cards" => 110_000,
        _ => 100_000
    };

    /// <param name="bulkType">"unique_artwork" (smaller, recommended) or "default_cards" (every printing).</param>
    public async Task BuildAsync(string bulkType, IProgress<IndexProgress> progress, CancellationToken ct)
    {
        progress.Report(new IndexProgress(0, 0, 0, null, false, "Fetching Scryfall bulk-data descriptor..."));
        var info = await _scryfall.GetBulkDataInfoAsync(bulkType, ct);
        if (info == null || string.IsNullOrEmpty(info.DownloadUri))
        {
            progress.Report(new IndexProgress(0, 0, 0, null, true, "Could not locate Scryfall bulk data."));
            return;
        }

        // 1) Download the bulk file to disk (reuse an intact prior download to resume cheaply).
        var tmp = Path.Combine(Path.GetTempPath(), $"cardscanner_bulk_{bulkType}.json");
        long sizeMb = info.Size / (1024 * 1024);
        if (File.Exists(tmp) && new FileInfo(tmp).Length == info.Size)
        {
            progress.Report(new IndexProgress(0, 0, 0, null, false, $"Reusing downloaded '{info.Name}' ({sizeMb} MB)."));
        }
        else
        {
            var byteProgress = new Progress<long>(b =>
                progress.Report(new IndexProgress(0, 0, 0, null, false,
                    $"Downloading '{info.Name}': {b / (1024 * 1024)} / {sizeMb} MB")));
            await _scryfall.DownloadBulkToFileAsync(info.DownloadUri, tmp, byteProgress, ct);
        }

        // 2) Parse from disk, hashing images with bounded concurrency.
        // "Complete" = both whole-card and art hashes present, so rows from an older index
        // that lack the art hash are re-processed and upgraded.
        var alreadyIndexed = _db.GetCompleteScryfallIds();
        int approx = ApproxCount(bulkType);
        int processed = 0, added = 0, skipped = 0;
        var batch = new List<ScryfallCardDto>(BatchSize);
        var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };

        progress.Report(new IndexProgress(0, 0, 0, null, false,
            $"Hashing images (0 / ~{approx:n0}). {alreadyIndexed.Count:n0} already indexed."));

        await using var fs = File.OpenRead(tmp);
        await foreach (var card in JsonSerializer.DeserializeAsyncEnumerable<ScryfallCardDto>(fs, options, ct))
        {
            ct.ThrowIfCancellationRequested();
            if (card == null) continue;
            processed++;

            if (string.IsNullOrEmpty(card.Id) || alreadyIndexed.Contains(card.Id) || string.IsNullOrEmpty(card.ImageUrl("small")))
            {
                skipped++;
            }
            else
            {
                batch.Add(card);
            }

            if (batch.Count >= BatchSize)
            {
                added += await FlushBatchAsync(batch, ct);
                batch.Clear();
                progress.Report(new IndexProgress(processed, added, skipped, card.Name, false,
                    $"Hashing images ({added:n0} added / ~{approx:n0}, {skipped:n0} skipped)"));
            }
        }

        if (batch.Count > 0)
            added += await FlushBatchAsync(batch, ct);

        progress.Report(new IndexProgress(processed, added, skipped, null, true,
            $"Index build complete. Added {added:n0}, skipped {skipped:n0}. Total in index: {_db.IndexCount():n0}."));

        try { File.Delete(tmp); } catch { /* leave temp for a possible retry */ }
    }

    /// <summary>Download + hash a batch of cards concurrently, then upsert. Returns count added.</summary>
    private async Task<int> FlushBatchAsync(List<ScryfallCardDto> cards, CancellationToken ct)
    {
        using var sem = new SemaphoreSlim(ImageConcurrency);
        var tasks = cards.Select(async card =>
        {
            await sem.WaitAsync(ct);
            try
            {
                var url = card.ImageUrl("small");
                if (string.IsNullOrEmpty(url)) return null;
                var bytes = await _scryfall.DownloadImageAsync(url, ct);
                if (bytes == null) return null;
                var (full, art) = _hasher.HashFullAndArt(bytes);
                return new CardIndexEntry
                {
                    ScryfallId = card.Id,
                    OracleId = card.OracleId ?? "",
                    Name = card.Name,
                    SetCode = card.Set,
                    CollectorNumber = card.CollectorNumber,
                    ImageUri = card.ImageUrl("normal"),
                    PerceptualHash = full,
                    ArtHash = art
                };
            }
            catch { return null; }
            finally { sem.Release(); }
        });

        var entries = (await Task.WhenAll(tasks)).Where(e => e != null).Select(e => e!).ToList();
        if (entries.Count > 0) _db.UpsertIndexEntries(entries);
        return entries.Count;
    }
}

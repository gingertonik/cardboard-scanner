using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading;
using System.Threading.Tasks;
using CardScanner.Models;

namespace CardScanner.Services;

// ---- Minimal DTOs for the parts of the Scryfall card object we use ----

public sealed class ScryfallCardDto
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("oracle_id")] public string? OracleId { get; set; }
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("set")] public string? Set { get; set; }
    [JsonPropertyName("set_name")] public string? SetName { get; set; }
    [JsonPropertyName("collector_number")] public string? CollectorNumber { get; set; }
    [JsonPropertyName("rarity")] public string? Rarity { get; set; }
    [JsonPropertyName("mana_cost")] public string? ManaCost { get; set; }
    [JsonPropertyName("type_line")] public string? TypeLine { get; set; }
    [JsonPropertyName("scryfall_uri")] public string? ScryfallUri { get; set; }
    [JsonPropertyName("layout")] public string? Layout { get; set; }
    [JsonPropertyName("image_uris")] public Dictionary<string, string>? ImageUris { get; set; }
    [JsonPropertyName("card_faces")] public List<ScryfallCardDto>? CardFaces { get; set; }
    [JsonPropertyName("prices")] public Dictionary<string, string?>? Prices { get; set; }

    /// <summary>Preferred image URL for display/hashing, walking card faces if needed.</summary>
    public string? ImageUrl(string size = "normal")
    {
        if (ImageUris != null && ImageUris.TryGetValue(size, out var u)) return u;
        if (ImageUris != null && ImageUris.TryGetValue("normal", out var n)) return n;
        if (CardFaces is { Count: > 0 })
        {
            var face = CardFaces[0];
            if (face.ImageUris != null && face.ImageUris.TryGetValue(size, out var fu)) return fu;
            if (face.ImageUris != null && face.ImageUris.TryGetValue("normal", out var fn)) return fn;
        }
        return null;
    }

    public ScannedCard ToScannedCard()
    {
        return new ScannedCard
        {
            ScryfallId = Id,
            Name = Name,
            SetCode = Set,
            SetName = SetName,
            CollectorNumber = CollectorNumber,
            Rarity = Rarity,
            ManaCost = string.IsNullOrEmpty(ManaCost) && CardFaces is { Count: > 0 } ? CardFaces[0].ManaCost : ManaCost,
            TypeLine = TypeLine,
            PriceUsd = ParsePrice("usd"),
            PriceUsdFoil = ParsePrice("usd_foil"),
            ImageUri = ImageUrl("normal"),
            ScryfallUri = ScryfallUri,
            ScannedAt = DateTimeOffset.Now
        };
    }

    private decimal? ParsePrice(string key)
    {
        if (Prices != null && Prices.TryGetValue(key, out var v) && !string.IsNullOrWhiteSpace(v)
            && decimal.TryParse(v, NumberStyles.Any, CultureInfo.InvariantCulture, out var p))
            return p;
        return null;
    }
}

public sealed class BulkDataInfo
{
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("download_uri")] public string DownloadUri { get; set; } = "";
    [JsonPropertyName("size")] public long Size { get; set; }
    [JsonPropertyName("name")] public string Name { get; set; } = "";
}

/// <summary>
/// Thin, rate-limited Scryfall API client. Follows Scryfall's request guidelines:
/// identifying User-Agent, Accept header, and 50-100 ms between API requests.
/// </summary>
public sealed class ScryfallClient : IDisposable
{
    private const string ApiBase = "https://api.scryfall.com";
    private readonly HttpClient _http;
    private readonly HttpClient _download; // no timeout — for the large bulk-data file
    private readonly SemaphoreSlim _gate = new(1, 1);
    private long _lastRequestTicks;

    /// <summary>Minimum spacing between api.scryfall.com requests.</summary>
    public TimeSpan MinApiInterval { get; set; } = TimeSpan.FromMilliseconds(100);

    public ScryfallClient()
    {
        _http = new HttpClient();
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("CardScanner/1.0 (+local MTG collection tool)");
        _http.DefaultRequestHeaders.Accept.ParseAdd("application/json;q=0.9,*/*;q=0.8");
        _http.Timeout = TimeSpan.FromSeconds(60);

        _download = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        _download.DefaultRequestHeaders.UserAgent.ParseAdd("CardScanner/1.0 (+local MTG collection tool)");
        _download.DefaultRequestHeaders.Accept.ParseAdd("application/json;q=0.9,*/*;q=0.8");
    }

    /// <summary>
    /// Download a bulk-data file to <paramref name="destPath"/>, reporting bytes received.
    /// Uses an untimed client so the large (hundreds of MB) transfer is not cut off.
    /// </summary>
    public async Task DownloadBulkToFileAsync(string uri, string destPath, IProgress<long>? bytes, CancellationToken ct = default)
    {
        using var resp = await _download.GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, ct);
        resp.EnsureSuccessStatusCode();
        await using var src = await resp.Content.ReadAsStreamAsync(ct);
        await using var dst = File.Create(destPath);
        var buffer = new byte[1 << 20];
        long total = 0;
        int read;
        while ((read = await src.ReadAsync(buffer, ct)) > 0)
        {
            await dst.WriteAsync(buffer.AsMemory(0, read), ct);
            total += read;
            bytes?.Report(total);
        }
    }

    private async Task ThrottleAsync(CancellationToken ct)
    {
        await _gate.WaitAsync(ct);
        try
        {
            long now = Environment.TickCount64;
            long elapsed = now - _lastRequestTicks;
            long waitMs = (long)MinApiInterval.TotalMilliseconds - elapsed;
            if (waitMs > 0) await Task.Delay((int)waitMs, ct);
            _lastRequestTicks = Environment.TickCount64;
        }
        finally { _gate.Release(); }
    }

    /// <summary>Fuzzy name lookup: GET /cards/named?fuzzy=... Returns null on no/ambiguous match.</summary>
    public async Task<ScannedCard?> GetByFuzzyNameAsync(string name, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(name)) return null;
        await ThrottleAsync(ct);
        var url = $"{ApiBase}/cards/named?fuzzy={Uri.EscapeDataString(name)}";
        using var resp = await _http.GetAsync(url, ct);
        if (resp.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.BadRequest)
            return null; // not found or ambiguous
        if (!resp.IsSuccessStatusCode) return null;
        var dto = await resp.Content.ReadFromJsonAsync<ScryfallCardDto>(cancellationToken: ct);
        return dto?.ToScannedCard();
    }

    /// <summary>Exact printing lookup by Scryfall id: GET /cards/{id}.</summary>
    public async Task<ScannedCard?> GetByIdAsync(string scryfallId, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(scryfallId)) return null;
        await ThrottleAsync(ct);
        var url = $"{ApiBase}/cards/{Uri.EscapeDataString(scryfallId)}";
        using var resp = await _http.GetAsync(url, ct);
        if (!resp.IsSuccessStatusCode) return null;
        var dto = await resp.Content.ReadFromJsonAsync<ScryfallCardDto>(cancellationToken: ct);
        return dto?.ToScannedCard();
    }

    /// <summary>General card search for the manual "add card" box. Returns distinct cards.</summary>
    public async Task<List<ScannedCard>> SearchAsync(string query, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(query)) return new();
        var url = $"{ApiBase}/cards/search?unique=cards&order=name&q={Uri.EscapeDataString(query)}";
        return await RunSearchAsync(url, ct);
    }

    /// <summary>All printings of a card by exact name (newest first), for printing disambiguation.</summary>
    public async Task<List<ScannedCard>> GetPrintingsAsync(string exactName, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(exactName)) return new();
        var q = $"!\"{exactName}\" unique:prints";
        var url = $"{ApiBase}/cards/search?unique=prints&order=released&dir=desc&q={Uri.EscapeDataString(q)}";
        return await RunSearchAsync(url, ct);
    }

    private async Task<List<ScannedCard>> RunSearchAsync(string? url, CancellationToken ct)
    {
        var results = new List<ScannedCard>();
        // Follow pagination a few pages at most (175 cards/page is ample for our uses).
        for (int page = 0; page < 4 && url != null; page++)
        {
            await ThrottleAsync(ct);
            using var resp = await _http.GetAsync(url, ct);
            if (resp.StatusCode == HttpStatusCode.NotFound) break; // no cards matched
            if (!resp.IsSuccessStatusCode) break;

            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
            if (doc.RootElement.TryGetProperty("data", out var data))
            {
                foreach (var item in data.EnumerateArray())
                {
                    var dto = item.Deserialize<ScryfallCardDto>();
                    if (dto != null) results.Add(dto.ToScannedCard());
                }
            }
            url = doc.RootElement.TryGetProperty("has_more", out var hm) && hm.GetBoolean()
                  && doc.RootElement.TryGetProperty("next_page", out var np)
                ? np.GetString()
                : null;
        }
        return results;
    }

    /// <summary>Locate a bulk-data descriptor, e.g. "unique_artwork" or "default_cards".</summary>
    public async Task<BulkDataInfo?> GetBulkDataInfoAsync(string type, CancellationToken ct = default)
    {
        await ThrottleAsync(ct);
        using var resp = await _http.GetAsync($"{ApiBase}/bulk-data", ct);
        if (!resp.IsSuccessStatusCode) return null;
        using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync(ct));
        if (!doc.RootElement.TryGetProperty("data", out var data)) return null;
        foreach (var item in data.EnumerateArray())
        {
            if (item.TryGetProperty("type", out var t) && t.GetString() == type)
            {
                return new BulkDataInfo
                {
                    Type = type,
                    DownloadUri = item.GetProperty("download_uri").GetString() ?? "",
                    Size = item.TryGetProperty("size", out var s) ? s.GetInt64() : 0,
                    Name = item.TryGetProperty("name", out var n) ? n.GetString() ?? "" : ""
                };
            }
        }
        return null;
    }

    /// <summary>Open the bulk-data JSON as a stream (served from a CDN, not rate-limited here).</summary>
    public async Task<Stream> OpenBulkStreamAsync(string downloadUri, CancellationToken ct = default)
    {
        var resp = await _http.GetAsync(downloadUri, HttpCompletionOption.ResponseHeadersRead, ct);
        resp.EnsureSuccessStatusCode();
        return await resp.Content.ReadAsStreamAsync(ct);
    }

    /// <summary>Download a single card image's bytes from the Scryfall image CDN.</summary>
    public async Task<byte[]?> DownloadImageAsync(string imageUri, CancellationToken ct = default)
    {
        try
        {
            using var resp = await _http.GetAsync(imageUri, ct);
            if (!resp.IsSuccessStatusCode) return null;
            return await resp.Content.ReadAsByteArrayAsync(ct);
        }
        catch { return null; }
    }

    public void Dispose()
    {
        _http.Dispose();
        _download.Dispose();
        _gate.Dispose();
    }
}

namespace CardScanner.Models;

/// <summary>
/// One printing from the local Scryfall match index (used for perceptual-hash fallback matching).
/// </summary>
public sealed class CardIndexEntry
{
    public required string ScryfallId { get; init; }
    public required string OracleId { get; init; }
    public required string Name { get; init; }
    public string? SetCode { get; init; }
    public string? CollectorNumber { get; init; }
    public string? ImageUri { get; init; }
    /// <summary>64-bit perceptual hash of the whole card image.</summary>
    public ulong PerceptualHash { get; init; }
    /// <summary>64-bit perceptual hash of the art window (0 if not computed yet).</summary>
    public ulong ArtHash { get; init; }
}

/// <summary>
/// A card the user has scanned and confirmed into their library. The per-copy editable
/// fields raise change notifications so the library grid stays in sync with edits.
/// </summary>
public sealed class ScannedCard : System.ComponentModel.INotifyPropertyChanged
{
    public event System.ComponentModel.PropertyChangedEventHandler? PropertyChanged;
    private void Raise(string n) => PropertyChanged?.Invoke(this, new System.ComponentModel.PropertyChangedEventArgs(n));

    public long Id { get; set; }
    public required string ScryfallId { get; set; }
    public required string Name { get; set; }
    public string? SetCode { get; set; }
    public string? SetName { get; set; }
    public string? CollectorNumber { get; set; }
    public string? Rarity { get; set; }
    public string? ManaCost { get; set; }
    public string? TypeLine { get; set; }
    public decimal? PriceUsd { get; set; }
    public decimal? PriceUsdFoil { get; set; }
    public string? ImageUri { get; set; }
    public string? ScryfallUri { get; set; }

    // Per-copy attributes that affect value and deckbuilding-site imports.
    private bool _foil;
    public bool Foil { get => _foil; set { if (_foil != value) { _foil = value; Raise(nameof(Foil)); Raise(nameof(EffectivePrice)); } } }

    private string _condition = "NM";                  // NM, LP, MP, HP, DMG
    public string Condition { get => _condition; set { if (_condition != value) { _condition = value; Raise(nameof(Condition)); } } }

    private string _language = "en";                   // Scryfall language code
    public string Language { get => _language; set { if (_language != value) { _language = value; Raise(nameof(Language)); } } }

    private int _quantity = 1;
    public int Quantity { get => _quantity; set { if (_quantity != value) { _quantity = value; Raise(nameof(Quantity)); } } }

    public DateTimeOffset ScannedAt { get; set; } = DateTimeOffset.Now;

    /// <summary>The price that applies to this copy given its finish.</summary>
    public decimal? EffectivePrice => Foil ? (PriceUsdFoil ?? PriceUsd) : PriceUsd;

    /// <summary>"Card Name (SET) 123" — the printing identity shown in the UI.</summary>
    public string PrintingLabel
    {
        get
        {
            var set = string.IsNullOrEmpty(SetCode) ? "" : $" ({SetCode.ToUpperInvariant()})";
            var num = string.IsNullOrEmpty(CollectorNumber) ? "" : $" {CollectorNumber}";
            return $"{Name}{set}{num}{(Foil ? " · Foil" : "")}";
        }
    }
}

/// <summary>Lightweight condition / language reference data for pickers.</summary>
public static class CardCatalog
{
    public static readonly string[] Conditions = { "NM", "LP", "MP", "HP", "DMG" };

    // Most common MTG print languages (Scryfall codes).
    public static readonly (string Code, string Name)[] Languages =
    {
        ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
        ("it", "Italian"), ("pt", "Portuguese"), ("ja", "Japanese"), ("ko", "Korean"),
        ("ru", "Russian"), ("zhs", "Chinese (Simplified)"), ("zht", "Chinese (Traditional)"),
    };
}

public enum MatchMethod
{
    None,
    OcrNameLookup,
    PerceptualHash,
    HybridConfirmed
}

/// <summary>
/// The result of trying to identify a detected card image.
/// </summary>
public sealed class MatchResult
{
    public bool Success { get; init; }
    public MatchMethod Method { get; init; }
    /// <summary>0..1 confidence estimate.</summary>
    public double Confidence { get; init; }
    public ScannedCard? Card { get; init; }
    /// <summary>Raw text OCR read from the title strip, for diagnostics.</summary>
    public string? OcrText { get; init; }
    public string? Notes { get; init; }

    public static MatchResult Fail(string? ocrText = null, string? notes = null) => new()
    {
        Success = false,
        Method = MatchMethod.None,
        Confidence = 0,
        OcrText = ocrText,
        Notes = notes
    };
}

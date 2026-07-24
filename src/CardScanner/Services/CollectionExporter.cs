using System.Collections.Generic;
using System.Globalization;
using System.Text;
using CardScanner.Models;

namespace CardScanner.Services;

public enum ExportFormat
{
    PlainTextList,
    MoxfieldText,
    MoxfieldCsv,
    ArchidektCsv,
    GenericCsv
}

/// <summary>
/// Serializes the owned-card library into text formats accepted by popular deckbuilding
/// sites. Moxfield conventions verified against its documented collection-CSV importer
/// (columns matched by name; Foil = "foil"/blank; full condition/language names).
/// </summary>
public static class CollectionExporter
{
    public static readonly IReadOnlyList<(ExportFormat Format, string Label)> All = new[]
    {
        (ExportFormat.MoxfieldText,  "Moxfield / Arena — deck text"),
        (ExportFormat.MoxfieldCsv,   "Moxfield — collection CSV"),
        (ExportFormat.ArchidektCsv,  "Archidekt — collection CSV"),
        (ExportFormat.PlainTextList, "Plain deck list (universal)"),
        (ExportFormat.GenericCsv,    "Generic CSV / spreadsheet"),
    };

    public static string Export(IEnumerable<ScannedCard> cards, ExportFormat format) => format switch
    {
        ExportFormat.PlainTextList => PlainText(cards),
        ExportFormat.MoxfieldText => MoxfieldText(cards),
        ExportFormat.MoxfieldCsv => MoxfieldCsv(cards),
        ExportFormat.ArchidektCsv => ArchidektCsv(cards),
        ExportFormat.GenericCsv => GenericCsv(cards),
        _ => PlainText(cards)
    };

    public static (string Extension, string SuggestedName) FileInfoFor(ExportFormat format) => format switch
    {
        ExportFormat.MoxfieldText => (".txt", "library-moxfield.txt"),
        ExportFormat.MoxfieldCsv => (".csv", "library-moxfield.csv"),
        ExportFormat.ArchidektCsv => (".csv", "library-archidekt.csv"),
        ExportFormat.GenericCsv => (".csv", "library.csv"),
        _ => (".txt", "library.txt")
    };

    // ---------------- text formats ----------------

    private static string PlainText(IEnumerable<ScannedCard> cards)
    {
        var sb = new StringBuilder();
        foreach (var c in cards)
            sb.Append(c.Quantity).Append(' ').AppendLine(c.Name);
        return sb.ToString();
    }

    /// <summary>"1 Lightning Bolt (2X2) 117 *F*" — accepted by Moxfield and MTG Arena import boxes.</summary>
    private static string MoxfieldText(IEnumerable<ScannedCard> cards)
    {
        var sb = new StringBuilder();
        foreach (var c in cards)
        {
            sb.Append(c.Quantity).Append(' ').Append(c.Name);
            if (!string.IsNullOrEmpty(c.SetCode) && !string.IsNullOrEmpty(c.CollectorNumber))
                sb.Append(" (").Append(c.SetCode!.ToUpperInvariant()).Append(") ").Append(c.CollectorNumber);
            if (c.Foil) sb.Append(" *F*");
            sb.AppendLine();
        }
        return sb.ToString();
    }

    // ---------------- CSV formats ----------------

    private static string MoxfieldCsv(IEnumerable<ScannedCard> cards)
    {
        var sb = new StringBuilder();
        sb.AppendLine("Count,Name,Edition,Condition,Language,Foil,Collector Number,Tag,Purchase Price");
        foreach (var c in cards)
        {
            sb.AppendLine(string.Join(",",
                Csv(c.Quantity.ToString(CultureInfo.InvariantCulture)),
                Csv(c.Name),
                Csv(c.SetCode?.ToLowerInvariant() ?? ""),
                Csv(MoxfieldCondition(c.Condition)),
                Csv(LanguageName(c.Language)),
                Csv(c.Foil ? "foil" : ""),
                Csv(c.CollectorNumber ?? ""),
                Csv(""),
                Csv(PriceString(c.EffectivePrice))));
        }
        return sb.ToString();
    }

    private static string ArchidektCsv(IEnumerable<ScannedCard> cards)
    {
        // Archidekt matches columns by header and can key on Scryfall ID for the exact printing.
        var sb = new StringBuilder();
        sb.AppendLine("Quantity,Name,Finish,Condition,Language,Edition Code,Collector Number,Scryfall ID,Purchase Price");
        foreach (var c in cards)
        {
            sb.AppendLine(string.Join(",",
                Csv(c.Quantity.ToString(CultureInfo.InvariantCulture)),
                Csv(c.Name),
                Csv(c.Foil ? "Foil" : "Normal"),
                Csv(c.Condition),
                Csv(LanguageName(c.Language)),
                Csv(c.SetCode?.ToLowerInvariant() ?? ""),
                Csv(c.CollectorNumber ?? ""),
                Csv(c.ScryfallId),
                Csv(PriceString(c.EffectivePrice))));
        }
        return sb.ToString();
    }

    private static string GenericCsv(IEnumerable<ScannedCard> cards)
    {
        var sb = new StringBuilder();
        sb.AppendLine("Count,Name,Set Code,Set Name,Collector Number,Rarity,Foil,Condition,Language,Price USD,Type,Scryfall ID,Scryfall URI,Scanned At");
        foreach (var c in cards)
        {
            sb.AppendLine(string.Join(",",
                Csv(c.Quantity.ToString(CultureInfo.InvariantCulture)),
                Csv(c.Name),
                Csv(c.SetCode?.ToUpperInvariant() ?? ""),
                Csv(c.SetName ?? ""),
                Csv(c.CollectorNumber ?? ""),
                Csv(c.Rarity ?? ""),
                Csv(c.Foil ? "true" : "false"),
                Csv(c.Condition),
                Csv(c.Language),
                Csv(PriceString(c.EffectivePrice)),
                Csv(c.TypeLine ?? ""),
                Csv(c.ScryfallId),
                Csv(c.ScryfallUri ?? ""),
                Csv(c.ScannedAt.ToString("yyyy-MM-dd"))));
        }
        return sb.ToString();
    }

    // ---------------- helpers ----------------

    private static string PriceString(decimal? p) => p?.ToString("0.00", CultureInfo.InvariantCulture) ?? "";

    private static string MoxfieldCondition(string cond) => cond.ToUpperInvariant() switch
    {
        "NM" => "Near Mint",
        "LP" => "Lightly Played",
        "MP" => "Played",
        "HP" => "Heavily Played",
        "DMG" => "Damaged",
        _ => "Near Mint"
    };

    private static string LanguageName(string code)
    {
        foreach (var (c, name) in CardCatalog.Languages)
            if (string.Equals(c, code, StringComparison.OrdinalIgnoreCase))
                return code switch { "zhs" => "Simplified Chinese", "zht" => "Traditional Chinese", _ => name };
        return "English";
    }

    /// <summary>RFC-4180 CSV field escaping.</summary>
    private static string Csv(string value)
    {
        if (value.IndexOfAny(new[] { ',', '"', '\n', '\r' }) < 0) return value;
        return "\"" + value.Replace("\"", "\"\"") + "\"";
    }
}

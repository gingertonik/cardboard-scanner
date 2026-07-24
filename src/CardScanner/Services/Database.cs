using System.Collections.Generic;
using System.IO;
using CardScanner.Models;
using Microsoft.Data.Sqlite;

namespace CardScanner.Services;

/// <summary>
/// SQLite-backed storage for both the user's card library and the local Scryfall
/// perceptual-hash match index. The DB file lives under %LOCALAPPDATA%\CardScanner.
/// </summary>
public sealed class Database
{
    private readonly string _connectionString;

    public string DbPath { get; }

    public Database(string? dbPath = null)
    {
        DbPath = dbPath ?? DefaultDbPath();
        Directory.CreateDirectory(Path.GetDirectoryName(DbPath)!);
        _connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = DbPath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Shared
        }.ToString();
        Initialize();
    }

    public static string DefaultDbPath()
    {
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "CardScanner");
        return Path.Combine(dir, "cardscanner.db");
    }

    private SqliteConnection Open()
    {
        var conn = new SqliteConnection(_connectionString);
        conn.Open();
        using var pragma = conn.CreateCommand();
        pragma.CommandText = "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;";
        pragma.ExecuteNonQuery();
        return conn;
    }

    private void Initialize()
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = """
            CREATE TABLE IF NOT EXISTS match_index (
                scryfall_id      TEXT PRIMARY KEY,
                oracle_id        TEXT,
                name             TEXT NOT NULL,
                set_code         TEXT,
                collector_number TEXT,
                image_uri        TEXT,
                phash            INTEGER NOT NULL,
                art_phash        INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_match_index_name ON match_index(name);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS collection (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id      TEXT NOT NULL,
                name             TEXT NOT NULL,
                set_code         TEXT,
                set_name         TEXT,
                collector_number TEXT,
                rarity           TEXT,
                mana_cost        TEXT,
                type_line        TEXT,
                price_usd        TEXT,
                price_usd_foil   TEXT,
                image_uri        TEXT,
                scryfall_uri     TEXT,
                foil             INTEGER NOT NULL DEFAULT 0,
                condition        TEXT NOT NULL DEFAULT 'NM',
                language         TEXT NOT NULL DEFAULT 'en',
                quantity         INTEGER NOT NULL DEFAULT 1,
                scanned_at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_collection_scryfall ON collection(scryfall_id);
            """;
        cmd.ExecuteNonQuery();

        MigrateColumns(conn, "collection", new[]
        {
            ("price_usd_foil", "ALTER TABLE collection ADD COLUMN price_usd_foil TEXT;"),
            ("foil",           "ALTER TABLE collection ADD COLUMN foil INTEGER NOT NULL DEFAULT 0;"),
            ("condition",      "ALTER TABLE collection ADD COLUMN condition TEXT NOT NULL DEFAULT 'NM';"),
            ("language",       "ALTER TABLE collection ADD COLUMN language TEXT NOT NULL DEFAULT 'en';"),
        });
        MigrateColumns(conn, "match_index", new[]
        {
            ("art_phash", "ALTER TABLE match_index ADD COLUMN art_phash INTEGER NOT NULL DEFAULT 0;"),
        });
    }

    /// <summary>Add columns to a table created by an earlier version, if missing.</summary>
    private static void MigrateColumns(SqliteConnection conn, string table, (string Col, string Ddl)[] adds)
    {
        var existing = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        using (var info = conn.CreateCommand())
        {
            info.CommandText = $"PRAGMA table_info({table});";
            using var r = info.ExecuteReader();
            while (r.Read()) existing.Add(r.GetString(1)); // column name
        }
        foreach (var (col, ddl) in adds)
        {
            if (existing.Contains(col)) continue;
            using var alter = conn.CreateCommand();
            alter.CommandText = ddl;
            alter.ExecuteNonQuery();
        }
    }

    // ---------------- Meta (key/value) ----------------

    public string? GetMeta(string key)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT value FROM meta WHERE key = $k;";
        cmd.Parameters.AddWithValue("$k", key);
        return cmd.ExecuteScalar() as string;
    }

    public void SetMeta(string key, string value)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "INSERT INTO meta (key, value) VALUES ($k, $v) ON CONFLICT(key) DO UPDATE SET value = excluded.value;";
        cmd.Parameters.AddWithValue("$k", key);
        cmd.Parameters.AddWithValue("$v", value);
        cmd.ExecuteNonQuery();
    }

    // ---------------- Match index ----------------

    public int IndexCount()
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT COUNT(*) FROM match_index;";
        return System.Convert.ToInt32(cmd.ExecuteScalar());
    }

    /// <summary>Ids that are fully hashed (both whole-card and art hashes present).</summary>
    public HashSet<string> GetCompleteScryfallIds()
    {
        var set = new HashSet<string>();
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT scryfall_id FROM match_index WHERE phash != 0 AND art_phash != 0;";
        using var r = cmd.ExecuteReader();
        while (r.Read()) set.Add(r.GetString(0));
        return set;
    }

    /// <summary>Bulk upsert index entries in a single transaction.</summary>
    public void UpsertIndexEntries(IEnumerable<CardIndexEntry> entries)
    {
        using var conn = Open();
        using var tx = conn.BeginTransaction();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = """
            INSERT INTO match_index (scryfall_id, oracle_id, name, set_code, collector_number, image_uri, phash, art_phash)
            VALUES ($id, $oracle, $name, $set, $num, $img, $phash, $art)
            ON CONFLICT(scryfall_id) DO UPDATE SET
                oracle_id=excluded.oracle_id, name=excluded.name, set_code=excluded.set_code,
                collector_number=excluded.collector_number, image_uri=excluded.image_uri,
                phash=excluded.phash, art_phash=excluded.art_phash;
            """;
        var pId = cmd.CreateParameter(); pId.ParameterName = "$id"; cmd.Parameters.Add(pId);
        var pOracle = cmd.CreateParameter(); pOracle.ParameterName = "$oracle"; cmd.Parameters.Add(pOracle);
        var pName = cmd.CreateParameter(); pName.ParameterName = "$name"; cmd.Parameters.Add(pName);
        var pSet = cmd.CreateParameter(); pSet.ParameterName = "$set"; cmd.Parameters.Add(pSet);
        var pNum = cmd.CreateParameter(); pNum.ParameterName = "$num"; cmd.Parameters.Add(pNum);
        var pImg = cmd.CreateParameter(); pImg.ParameterName = "$img"; cmd.Parameters.Add(pImg);
        var pHash = cmd.CreateParameter(); pHash.ParameterName = "$phash"; cmd.Parameters.Add(pHash);
        var pArt = cmd.CreateParameter(); pArt.ParameterName = "$art"; cmd.Parameters.Add(pArt);

        foreach (var e in entries)
        {
            pId.Value = e.ScryfallId;
            pOracle.Value = (object?)e.OracleId ?? System.DBNull.Value;
            pName.Value = e.Name;
            pSet.Value = (object?)e.SetCode ?? System.DBNull.Value;
            pNum.Value = (object?)e.CollectorNumber ?? System.DBNull.Value;
            pImg.Value = (object?)e.ImageUri ?? System.DBNull.Value;
            // SQLite INTEGER is signed 64-bit; store the unsigned hash's bit pattern.
            pHash.Value = unchecked((long)e.PerceptualHash);
            pArt.Value = unchecked((long)e.ArtHash);
            cmd.ExecuteNonQuery();
        }
        tx.Commit();
    }

    /// <summary>Load the full index into memory for fast Hamming-distance search.</summary>
    public List<CardIndexEntry> LoadIndex()
    {
        var list = new List<CardIndexEntry>();
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT scryfall_id, oracle_id, name, set_code, collector_number, image_uri, phash, art_phash FROM match_index;";
        using var r = cmd.ExecuteReader();
        while (r.Read())
        {
            list.Add(new CardIndexEntry
            {
                ScryfallId = r.GetString(0),
                OracleId = r.IsDBNull(1) ? "" : r.GetString(1),
                Name = r.GetString(2),
                SetCode = r.IsDBNull(3) ? null : r.GetString(3),
                CollectorNumber = r.IsDBNull(4) ? null : r.GetString(4),
                ImageUri = r.IsDBNull(5) ? null : r.GetString(5),
                PerceptualHash = unchecked((ulong)r.GetInt64(6)),
                ArtHash = r.IsDBNull(7) ? 0UL : unchecked((ulong)r.GetInt64(7))
            });
        }
        return list;
    }

    // ---------------- Collection ----------------

    /// <summary>
    /// Add a scanned card. A "copy" is identified by printing + finish + condition + language;
    /// an identical copy increments quantity, otherwise a new row is created.
    /// Returns the resulting row's quantity.
    /// </summary>
    public int AddOrIncrement(ScannedCard card)
    {
        using var conn = Open();
        using var find = conn.CreateCommand();
        find.CommandText = """
            SELECT id, quantity FROM collection
            WHERE scryfall_id = $id AND foil = $foil AND condition = $cond AND language = $lang
            LIMIT 1;
            """;
        find.Parameters.AddWithValue("$id", card.ScryfallId);
        find.Parameters.AddWithValue("$foil", card.Foil ? 1 : 0);
        find.Parameters.AddWithValue("$cond", card.Condition);
        find.Parameters.AddWithValue("$lang", card.Language);
        using (var r = find.ExecuteReader())
        {
            if (r.Read())
            {
                long id = r.GetInt64(0);
                int qty = r.GetInt32(1) + 1;
                r.Close();
                using var upd = conn.CreateCommand();
                upd.CommandText = "UPDATE collection SET quantity=$q, scanned_at=$t WHERE id=$id;";
                upd.Parameters.AddWithValue("$q", qty);
                upd.Parameters.AddWithValue("$t", card.ScannedAt.ToString("o"));
                upd.Parameters.AddWithValue("$id", id);
                upd.ExecuteNonQuery();
                card.Id = id;
                card.Quantity = qty;
                return qty;
            }
        }

        using var ins = conn.CreateCommand();
        ins.CommandText = """
            INSERT INTO collection
                (scryfall_id, name, set_code, set_name, collector_number, rarity, mana_cost,
                 type_line, price_usd, price_usd_foil, image_uri, scryfall_uri,
                 foil, condition, language, quantity, scanned_at)
            VALUES ($sid, $name, $set, $setname, $num, $rarity, $mana, $type, $price, $pricef, $img, $uri,
                    $foil, $cond, $lang, 1, $t);
            SELECT last_insert_rowid();
            """;
        ins.Parameters.AddWithValue("$sid", card.ScryfallId);
        ins.Parameters.AddWithValue("$name", card.Name);
        ins.Parameters.AddWithValue("$set", (object?)card.SetCode ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$setname", (object?)card.SetName ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$num", (object?)card.CollectorNumber ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$rarity", (object?)card.Rarity ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$mana", (object?)card.ManaCost ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$type", (object?)card.TypeLine ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$price", (object?)card.PriceUsd?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$pricef", (object?)card.PriceUsdFoil?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$img", (object?)card.ImageUri ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$uri", (object?)card.ScryfallUri ?? System.DBNull.Value);
        ins.Parameters.AddWithValue("$foil", card.Foil ? 1 : 0);
        ins.Parameters.AddWithValue("$cond", card.Condition);
        ins.Parameters.AddWithValue("$lang", card.Language);
        ins.Parameters.AddWithValue("$t", card.ScannedAt.ToString("o"));
        card.Id = (long)ins.ExecuteScalar()!;
        card.Quantity = 1;
        return 1;
    }

    public List<ScannedCard> GetCollection()
    {
        var list = new List<ScannedCard>();
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = """
            SELECT id, scryfall_id, name, set_code, set_name, collector_number, rarity, mana_cost,
                   type_line, price_usd, price_usd_foil, image_uri, scryfall_uri,
                   foil, condition, language, quantity, scanned_at
            FROM collection ORDER BY scanned_at DESC;
            """;
        using var r = cmd.ExecuteReader();
        while (r.Read())
        {
            list.Add(new ScannedCard
            {
                Id = r.GetInt64(0),
                ScryfallId = r.GetString(1),
                Name = r.GetString(2),
                SetCode = r.IsDBNull(3) ? null : r.GetString(3),
                SetName = r.IsDBNull(4) ? null : r.GetString(4),
                CollectorNumber = r.IsDBNull(5) ? null : r.GetString(5),
                Rarity = r.IsDBNull(6) ? null : r.GetString(6),
                ManaCost = r.IsDBNull(7) ? null : r.GetString(7),
                TypeLine = r.IsDBNull(8) ? null : r.GetString(8),
                PriceUsd = r.IsDBNull(9) ? null : decimal.Parse(r.GetString(9), System.Globalization.CultureInfo.InvariantCulture),
                PriceUsdFoil = r.IsDBNull(10) ? null : decimal.Parse(r.GetString(10), System.Globalization.CultureInfo.InvariantCulture),
                ImageUri = r.IsDBNull(11) ? null : r.GetString(11),
                ScryfallUri = r.IsDBNull(12) ? null : r.GetString(12),
                Foil = !r.IsDBNull(13) && r.GetInt64(13) != 0,
                Condition = r.IsDBNull(14) ? "NM" : r.GetString(14),
                Language = r.IsDBNull(15) ? "en" : r.GetString(15),
                Quantity = r.GetInt32(16),
                ScannedAt = DateTimeOffset.Parse(r.GetString(17), System.Globalization.CultureInfo.InvariantCulture)
            });
        }
        return list;
    }

    public void RemoveOne(long id)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = """
            UPDATE collection SET quantity = quantity - 1 WHERE id = $id;
            DELETE FROM collection WHERE id = $id AND quantity <= 0;
            """;
        cmd.Parameters.AddWithValue("$id", id);
        cmd.ExecuteNonQuery();
    }

    /// <summary>Delete an entire row regardless of quantity.</summary>
    public void DeleteRow(long id)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "DELETE FROM collection WHERE id = $id;";
        cmd.Parameters.AddWithValue("$id", id);
        cmd.ExecuteNonQuery();
    }

    /// <summary>Set an explicit quantity; a value &lt;= 0 removes the row.</summary>
    public void SetQuantity(long id, int quantity)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        if (quantity <= 0)
        {
            cmd.CommandText = "DELETE FROM collection WHERE id = $id;";
            cmd.Parameters.AddWithValue("$id", id);
        }
        else
        {
            cmd.CommandText = "UPDATE collection SET quantity = $q WHERE id = $id;";
            cmd.Parameters.AddWithValue("$q", quantity);
            cmd.Parameters.AddWithValue("$id", id);
        }
        cmd.ExecuteNonQuery();
    }

    /// <summary>Update the editable per-copy attributes of a row.</summary>
    public void UpdateAttributes(long id, bool foil, string condition, string language)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "UPDATE collection SET foil=$foil, condition=$cond, language=$lang WHERE id=$id;";
        cmd.Parameters.AddWithValue("$foil", foil ? 1 : 0);
        cmd.Parameters.AddWithValue("$cond", condition);
        cmd.Parameters.AddWithValue("$lang", language);
        cmd.Parameters.AddWithValue("$id", id);
        cmd.ExecuteNonQuery();
    }

    /// <summary>Refresh stored prices for a printing across the whole collection.</summary>
    public void UpdatePrices(string scryfallId, decimal? usd, decimal? usdFoil)
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "UPDATE collection SET price_usd=$p, price_usd_foil=$pf WHERE scryfall_id=$id;";
        cmd.Parameters.AddWithValue("$p", (object?)usd?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? System.DBNull.Value);
        cmd.Parameters.AddWithValue("$pf", (object?)usdFoil?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? System.DBNull.Value);
        cmd.Parameters.AddWithValue("$id", scryfallId);
        cmd.ExecuteNonQuery();
    }

    /// <summary>Distinct Scryfall printing ids present in the collection (for price refresh).</summary>
    public List<string> DistinctScryfallIds()
    {
        var ids = new List<string>();
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT DISTINCT scryfall_id FROM collection;";
        using var r = cmd.ExecuteReader();
        while (r.Read()) ids.Add(r.GetString(0));
        return ids;
    }

    public int CollectionTotalCards()
    {
        using var conn = Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = "SELECT COALESCE(SUM(quantity),0) FROM collection;";
        return System.Convert.ToInt32(cmd.ExecuteScalar());
    }
}

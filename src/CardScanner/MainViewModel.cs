using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CardScanner.Models;
using CardScanner.Services;
using Microsoft.Win32;
using OpenCvSharp;
using OpenCvSharp.WpfExtensions;

namespace CardScanner;

public sealed record ExportOption(ExportFormat Format, string Label);
public sealed record LanguageOption(string Code, string Name);

public sealed class MainViewModel : ViewModelBase, IDisposable
{
    private readonly CameraService _camera = new();
    private readonly CardDetector _detector = new();
    private readonly OcrService _ocr = new();
    private readonly PerceptualHasher _hasher = new();
    private readonly Database _db;
    private readonly ScryfallClient _scryfall = new();
    private readonly CardMatcher _matcher;
    private readonly IndexBuilder _indexBuilder;
    private readonly System.Windows.Threading.Dispatcher _ui;
    private readonly ICollectionView _collectionView;

    private int _processing;
    private long _lastProcessTick;
    private const int ProcessIntervalMs = 200;
    private volatile float _zoom = 1.0f; // digital zoom read on the capture thread

    private string? _stableId;
    private int _stableCount;
    private string? _lastAddedId;
    private long _lastAddedTick;

    private CancellationTokenSource? _buildCts;
    private bool _connecting; // true while opening a device off the UI thread

    // current-match / printing state
    private ScannedCard? _currentCard;
    private double _lastConfidence;
    private string? _printingsForName;
    private bool _suppressPrinting;

    public MainViewModel()
    {
        _ui = Application.Current.Dispatcher;
        _db = new Database();
        _matcher = new CardMatcher(_db, _scryfall, _hasher);
        _indexBuilder = new IndexBuilder(_db, _scryfall, _hasher);

        _camera.FrameReady += OnFrameReady;
        _camera.Error += OnCameraError;

        StartCommand = new RelayCommand(Start, () => !IsScanning && !_connecting);
        StopCommand = new RelayCommand(Stop, () => IsScanning);
        AddCurrentCommand = new RelayCommand(AddCurrent, () => _currentCard != null);
        RemoveSelectedCommand = new RelayCommand(() => AdjustSelected(-1), () => SelectedCard != null);
        IncrementSelectedCommand = new RelayCommand(() => AdjustSelected(+1), () => SelectedCard != null);
        DeleteSelectedCommand = new RelayCommand(DeleteSelected, () => SelectedCard != null);
        RefreshDevicesCommand = new RelayCommand(() => _ = RefreshDevicesAsync());
        BuildIndexCommand = new RelayCommand(() => _ = BuildIndexAsync("unique_artwork"), () => !IsBuildingIndex);
        BuildFullIndexCommand = new RelayCommand(() => _ = BuildIndexAsync("default_cards"), () => !IsBuildingIndex);
        CancelBuildCommand = new RelayCommand(() => _buildCts?.Cancel(), () => IsBuildingIndex);
        RefocusCommand = new RelayCommand(() => _camera.TriggerRefocus());
        CameraSettingsCommand = new RelayCommand(() => _camera.OpenNativeSettings());
        OpenScryfallCommand = new RelayCommand(OpenScryfall, () => _currentCard?.ScryfallUri != null);
        SearchCommand = new RelayCommand(() => _ = SearchAsync(), () => !string.IsNullOrWhiteSpace(SearchQuery));
        ExportFileCommand = new RelayCommand(ExportToFile);
        CopyExportCommand = new RelayCommand(CopyExport);

        ExportOptions = CollectionExporter.All.Select(x => new ExportOption(x.Format, x.Label)).ToArray();
        SelectedExportOption = ExportOptions[0];
        LanguageOptions = CardCatalog.Languages.Select(l => new LanguageOption(l.Code, l.Name)).ToArray();

        _collectionView = CollectionViewSource.GetDefaultView(Collection);
        _collectionView.Filter = FilterRow;

        _autoUpdateIndex = _db.GetMeta("auto_index") != "0"; // default on

        _matcher.ReloadIndex();
        LoadCollection();
        UpdateIndexStatus();
        _ = RefreshDevicesAsync();
        MaybeAutoUpdateIndex();
    }

    /// <summary>How long before the index is considered stale and re-synced with Scryfall.</summary>
    private static readonly TimeSpan IndexRefreshInterval = TimeSpan.FromDays(7);

    // ---------------- Commands ----------------
    public ICommand StartCommand { get; }
    public ICommand StopCommand { get; }
    public ICommand AddCurrentCommand { get; }
    public ICommand RemoveSelectedCommand { get; }
    public ICommand IncrementSelectedCommand { get; }
    public ICommand DeleteSelectedCommand { get; }
    public ICommand RefreshDevicesCommand { get; }
    public ICommand BuildIndexCommand { get; }
    public ICommand BuildFullIndexCommand { get; }
    public ICommand CancelBuildCommand { get; }
    public ICommand OpenScryfallCommand { get; }
    public ICommand SearchCommand { get; }
    public ICommand ExportFileCommand { get; }
    public ICommand CopyExportCommand { get; }
    public ICommand RefocusCommand { get; }
    public ICommand CameraSettingsCommand { get; }

    // ---------------- Bindable collections ----------------
    public ObservableCollection<CameraDevice> Devices { get; } = new();
    public ObservableCollection<ScannedCard> Collection { get; } = new();
    public ObservableCollection<ScannedCard> SearchResults { get; } = new();
    public ObservableCollection<ScannedCard> Printings { get; } = new();

    public string[] Conditions => CardCatalog.Conditions;
    public LanguageOption[] LanguageOptions { get; }
    public ExportOption[] ExportOptions { get; }

    // ---------------- Simple bindable state ----------------
    private CameraDevice? _selectedDevice;
    public CameraDevice? SelectedDevice { get => _selectedDevice; set => Set(ref _selectedDevice, value); }

    private ScannedCard? _selectedCard;
    public ScannedCard? SelectedCard
    {
        get => _selectedCard;
        set
        {
            var old = _selectedCard;
            if (Set(ref _selectedCard, value))
            {
                // Auto-persist edits made to the selected row via the edit panel.
                if (old != null) old.PropertyChanged -= OnSelectedCardEdited;
                if (value != null) value.PropertyChanged += OnSelectedCardEdited;
                (RemoveSelectedCommand as RelayCommand)?.RaiseCanExecuteChanged();
                (IncrementSelectedCommand as RelayCommand)?.RaiseCanExecuteChanged();
                (DeleteSelectedCommand as RelayCommand)?.RaiseCanExecuteChanged();
                Raise(nameof(HasSelection));
            }
        }
    }

    /// <summary>True when a library row is selected (drives the edit panel's enabled state).</summary>
    public bool HasSelection => _selectedCard != null;

    private void OnSelectedCardEdited(object? sender, PropertyChangedEventArgs e)
    {
        if (sender is not ScannedCard c) return;
        if (e.PropertyName is not (nameof(ScannedCard.Quantity) or nameof(ScannedCard.Foil)
            or nameof(ScannedCard.Condition) or nameof(ScannedCard.Language))) return;

        if (c.Quantity <= 0)
        {
            _db.DeleteRow(c.Id);
            Collection.Remove(c);
        }
        else
        {
            _db.SetQuantity(c.Id, c.Quantity);
            _db.UpdateAttributes(c.Id, c.Foil, c.Condition, c.Language);
        }
        UpdateSummary();
    }

    private bool _isScanning;
    public bool IsScanning
    {
        get => _isScanning;
        set
        {
            if (Set(ref _isScanning, value))
            {
                (StartCommand as RelayCommand)?.RaiseCanExecuteChanged();
                (StopCommand as RelayCommand)?.RaiseCanExecuteChanged();
            }
        }
    }

    private bool _autoAdd = true;
    public bool AutoAdd { get => _autoAdd; set => Set(ref _autoAdd, value); }

    private double _zoomLevel = 1.0;
    public double Zoom
    {
        get => _zoomLevel;
        set
        {
            double v = Math.Clamp(value, 1.0, 4.0);
            if (Set(ref _zoomLevel, v)) { _zoom = (float)v; Raise(nameof(ZoomText)); }
        }
    }
    public string ZoomText => $"{_zoomLevel:0.0}×";

    private bool _autoFocus = true;
    public bool AutoFocus
    {
        get => _autoFocus;
        set
        {
            if (Set(ref _autoFocus, value))
            {
                _camera.SetAutoFocus(value);
                Raise(nameof(ManualFocusEnabled));
            }
        }
    }
    /// <summary>Manual focus slider is usable only when autofocus is off.</summary>
    public bool ManualFocusEnabled => !_autoFocus;

    private double _focusValue = 128;
    public double FocusValue
    {
        get => _focusValue;
        set { if (Set(ref _focusValue, value)) _camera.SetFocus(value); }
    }

    private ImageSource? _preview;
    public ImageSource? Preview { get => _preview; set => Set(ref _preview, value); }

    private ImageSource? _matchImage;
    public ImageSource? MatchImage { get => _matchImage; set => Set(ref _matchImage, value); }

    private string _status = "Ready. Select a device and press Start.";
    public string Status { get => _status; set => Set(ref _status, value); }

    private string _matchName = "—";
    public string MatchName { get => _matchName; set => Set(ref _matchName, value); }

    private string _matchDetails = "";
    public string MatchDetails { get => _matchDetails; set => Set(ref _matchDetails, value); }

    private string _matchMethod = "";
    public string MatchMethod { get => _matchMethod; set => Set(ref _matchMethod, value); }

    private double _confidence;
    public double Confidence { get => _confidence; set { if (Set(ref _confidence, value)) Raise(nameof(ConfidencePercent)); } }
    public string ConfidencePercent => $"{_confidence * 100:0}%";

    // per-copy pickers (sticky across scans)
    private bool _currentFoil;
    public bool CurrentFoil { get => _currentFoil; set => Set(ref _currentFoil, value); }

    private string _currentCondition = "NM";
    public string CurrentCondition { get => _currentCondition; set => Set(ref _currentCondition, value); }

    private string _currentLanguage = "en";
    public string CurrentLanguage { get => _currentLanguage; set => Set(ref _currentLanguage, value); }

    private ScannedCard? _selectedPrinting;
    public ScannedCard? SelectedPrinting
    {
        get => _selectedPrinting;
        set { if (Set(ref _selectedPrinting, value) && !_suppressPrinting && value != null) ApplyPrinting(value); }
    }

    private bool _printingsLoading;
    public bool PrintingsLoading { get => _printingsLoading; set => Set(ref _printingsLoading, value); }

    // search
    private string _searchQuery = "";
    public string SearchQuery
    {
        get => _searchQuery;
        set { if (Set(ref _searchQuery, value)) (SearchCommand as RelayCommand)?.RaiseCanExecuteChanged(); }
    }

    private bool _isSearching;
    public bool IsSearching { get => _isSearching; set => Set(ref _isSearching, value); }

    private ScannedCard? _selectedSearchResult;
    public ScannedCard? SelectedSearchResult
    {
        get => _selectedSearchResult;
        set { if (Set(ref _selectedSearchResult, value) && value != null) SetCurrentCard(value, "Manual (Scryfall search)", 1.0); }
    }

    // library filter + export
    private string _filterText = "";
    public string FilterText { get => _filterText; set { if (Set(ref _filterText, value)) _collectionView.Refresh(); } }

    public ExportOption SelectedExportOption { get; set; }

    private string _indexStatus = "";
    public string IndexStatus { get => _indexStatus; set => Set(ref _indexStatus, value); }

    private bool _autoUpdateIndex;
    public bool AutoUpdateIndex
    {
        get => _autoUpdateIndex;
        set
        {
            if (Set(ref _autoUpdateIndex, value))
            {
                _db.SetMeta("auto_index", value ? "1" : "0");
                if (value) MaybeAutoUpdateIndex();
            }
        }
    }

    private bool _isBuildingIndex;
    public bool IsBuildingIndex
    {
        get => _isBuildingIndex;
        set
        {
            if (Set(ref _isBuildingIndex, value))
            {
                (BuildIndexCommand as RelayCommand)?.RaiseCanExecuteChanged();
                (BuildFullIndexCommand as RelayCommand)?.RaiseCanExecuteChanged();
                (CancelBuildCommand as RelayCommand)?.RaiseCanExecuteChanged();
            }
        }
    }

    private string _buildProgress = "";
    public string BuildProgress { get => _buildProgress; set => Set(ref _buildProgress, value); }

    private string _collectionSummary = "";
    public string CollectionSummary { get => _collectionSummary; set => Set(ref _collectionSummary, value); }

    // ---------------- Camera control ----------------
    private async void Start()
    {
        var device = SelectedDevice;
        if (device == null)
        {
            MessageBox.Show("Select a video device first.", "No device selected",
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (_connecting || IsScanning) return;

        // Opening a busy device (esp. via DirectShow) can block for seconds, so do it off the
        // UI thread — the window stays responsive and shows "Connecting…" until we know.
        _connecting = true;
        Status = $"Connecting to “{device.Name}”…";
        (StartCommand as RelayCommand)?.RaiseCanExecuteChanged();

        bool ok;
        try { ok = await Task.Run(() => _camera.Start(device.Index)); }
        catch { ok = false; }
        finally
        {
            _connecting = false;
            (StartCommand as RelayCommand)?.RaiseCanExecuteChanged();
        }

        if (ok)
        {
            IsScanning = true;
            Status = $"Scanning “{device.Name}” ({_camera.FrameWidth}x{_camera.FrameHeight}). Hold a card up to the camera.";
        }
        else
        {
            IsScanning = false;
            Status = $"“{device.Name}” is unavailable — it may be in use by another app.";
            MessageBox.Show(
                _camera.LastError ?? $"Could not start “{device.Name}”.",
                "Camera unavailable", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private void OnCameraError(object? sender, string message)
    {
        // Raised from the capture thread if the device drops mid-session.
        _ = _ui.BeginInvoke(() =>
        {
            if (IsScanning) Stop();
            Status = "Camera stopped — device error.";
            MessageBox.Show(message, "Camera error", MessageBoxButton.OK, MessageBoxImage.Warning);
        });
    }

    private void Stop()
    {
        _camera.Stop();
        IsScanning = false;
        Status = "Stopped.";
    }

    private void OnFrameReady(object? sender, Mat frame)
    {
        Mat? zoomed = null;
        try
        {
            // Digital zoom: crop a centered region and let it scale up. Applied to both the
            // preview and the detection input, so a small overhead card fills more of the frame.
            Mat view = frame;
            float z = _zoom;
            if (z > 1.01f)
            {
                int cw = Math.Max(32, (int)(frame.Width / z));
                int ch = Math.Max(32, (int)(frame.Height / z));
                int x = (frame.Width - cw) / 2;
                int y = (frame.Height - ch) / 2;
                zoomed = new Mat(frame, new OpenCvSharp.Rect(x, y, cw, ch)).Clone();
                view = zoomed;
            }

            long now = Environment.TickCount64;
            bool acquired = Interlocked.CompareExchange(ref _processing, 1, 0) == 0;
            if (acquired)
            {
                if (now - _lastProcessTick >= ProcessIntervalMs)
                {
                    _lastProcessTick = now;
                    var procFrame = view.Clone();
                    _ = Task.Run(() => ProcessFrameAsync(procFrame)); // resets the guard in its finally
                }
                else
                {
                    Interlocked.Exchange(ref _processing, 0);
                }
            }

            BitmapSource bmp = view.ToBitmapSource();
            bmp.Freeze();
            _ = _ui.BeginInvoke(() => Preview = bmp);
        }
        catch { /* transient conversion errors are non-fatal */ }
        finally
        {
            zoomed?.Dispose();
            frame.Dispose();
        }
    }

    private async Task ProcessFrameAsync(Mat frame)
    {
        try
        {
            using var detection = _detector.Detect(frame);
            if (!detection.Found || detection.Warped == null)
            {
                _ = _ui.BeginInvoke(() => Status = IsScanning ? "Searching for a card..." : Status);
                return;
            }

            string ocrName = _ocr.Available ? await _ocr.ReadTitleAsync(detection.Warped) : string.Empty;
            MatchResult result = await _matcher.IdentifyAsync(detection.Warped, ocrName);
            _ = _ui.BeginInvoke(() => OnMatch(result));
        }
        catch (Exception ex)
        {
            _ = _ui.BeginInvoke(() => Status = "Processing error: " + ex.Message);
        }
        finally
        {
            frame.Dispose();
            Interlocked.Exchange(ref _processing, 0);
        }
    }

    // ---------------- Match handling (UI thread) ----------------
    private void OnMatch(MatchResult result)
    {
        if (!result.Success || result.Card == null)
        {
            _stableId = null;
            _stableCount = 0;
            Status = result.Notes ?? "No match.";
            return;
        }

        var card = result.Card;
        string method = result.Method + (string.IsNullOrEmpty(result.OcrText) ? "" : $"  ·  read: \"{result.OcrText}\"");
        SetCurrentCard(card, method, result.Confidence);

        if (_stableId == card.ScryfallId) _stableCount++;
        else { _stableId = card.ScryfallId; _stableCount = 1; }

        long now = Environment.TickCount64;
        bool cooldownPassed = _lastAddedId != card.ScryfallId || now - _lastAddedTick > 3000;

        if (AutoAdd && result.Confidence >= 0.80 && _stableCount >= 2 && cooldownPassed)
        {
            AddCard(card);
            _lastAddedId = card.ScryfallId;
            _lastAddedTick = now;
        }
        else
        {
            Status = $"Match: {card.Name} ({ConfidencePercent}). " +
                     (AutoAdd ? "Hold steady to auto-add…" : "Press \"Add to library\".");
        }
    }

    /// <summary>Central place that makes a card the "current" one and loads its printings.</summary>
    private void SetCurrentCard(ScannedCard card, string methodText, double confidence)
    {
        _currentCard = card;
        _lastConfidence = confidence;
        UpdateMatchDisplay(card, methodText, confidence);
        (AddCurrentCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (OpenScryfallCommand as RelayCommand)?.RaiseCanExecuteChanged();
        EnsurePrintingsFor(card);
    }

    private void UpdateMatchDisplay(ScannedCard card, string methodText, double confidence)
    {
        MatchName = card.Name;
        MatchDetails = FormatDetails(card);
        MatchMethod = methodText;
        Confidence = confidence;
        MatchImage = LoadRemoteImage(card.ImageUri);
    }

    private void EnsurePrintingsFor(ScannedCard card)
    {
        if (string.Equals(card.Name, _printingsForName, StringComparison.Ordinal))
        {
            // Same card name already loaded — just reflect the selected printing.
            _suppressPrinting = true;
            SelectedPrinting = Printings.FirstOrDefault(p => p.ScryfallId == card.ScryfallId);
            _suppressPrinting = false;
            return;
        }
        _ = LoadPrintingsAsync(card.Name, card.ScryfallId);
    }

    private async Task LoadPrintingsAsync(string name, string preferId)
    {
        PrintingsLoading = true;
        List<ScannedCard> list;
        try { list = await _scryfall.GetPrintingsAsync(name); }
        catch { list = new(); }

        _ = _ui.BeginInvoke(() =>
        {
            _printingsForName = name;
            Printings.Clear();
            foreach (var p in list) Printings.Add(p);
            _suppressPrinting = true;
            SelectedPrinting = Printings.FirstOrDefault(p => p.ScryfallId == preferId) ?? Printings.FirstOrDefault();
            _suppressPrinting = false;
            PrintingsLoading = false;
        });
    }

    private void ApplyPrinting(ScannedCard printing)
    {
        _currentCard = printing;
        _lastConfidence = _lastConfidence <= 0 ? 1.0 : _lastConfidence;
        UpdateMatchDisplay(printing, "Printing selected", _lastConfidence);
        (AddCurrentCommand as RelayCommand)?.RaiseCanExecuteChanged();
        (OpenScryfallCommand as RelayCommand)?.RaiseCanExecuteChanged();
    }

    private static string FormatDetails(ScannedCard c)
    {
        var parts = new List<string>();
        if (!string.IsNullOrEmpty(c.TypeLine)) parts.Add(c.TypeLine!);
        if (!string.IsNullOrEmpty(c.SetName)) parts.Add($"{c.SetName} ({c.SetCode?.ToUpperInvariant()}) #{c.CollectorNumber}");
        else if (!string.IsNullOrEmpty(c.SetCode)) parts.Add($"{c.SetCode?.ToUpperInvariant()} #{c.CollectorNumber}");
        if (!string.IsNullOrEmpty(c.Rarity)) parts.Add(char.ToUpperInvariant(c.Rarity![0]) + c.Rarity[1..]);
        if (c.PriceUsd is { } p) parts.Add($"${p:0.00}");
        if (c.PriceUsdFoil is { } pf) parts.Add($"foil ${pf:0.00}");
        return string.Join("   ·   ", parts);
    }

    // ---------------- Add / library edit ----------------
    private void AddCurrent()
    {
        if (_currentCard != null) AddCard(_currentCard);
    }

    private void AddCard(ScannedCard card)
    {
        var copy = new ScannedCard
        {
            ScryfallId = card.ScryfallId,
            Name = card.Name,
            SetCode = card.SetCode,
            SetName = card.SetName,
            CollectorNumber = card.CollectorNumber,
            Rarity = card.Rarity,
            ManaCost = card.ManaCost,
            TypeLine = card.TypeLine,
            PriceUsd = card.PriceUsd,
            PriceUsdFoil = card.PriceUsdFoil,
            ImageUri = card.ImageUri,
            ScryfallUri = card.ScryfallUri,
            Foil = CurrentFoil,
            Condition = CurrentCondition,
            Language = CurrentLanguage,
            ScannedAt = DateTimeOffset.Now
        };
        int qty = _db.AddOrIncrement(copy);
        LoadCollection();
        string finish = CurrentFoil ? " foil" : "";
        Status = qty > 1
            ? $"Added another {card.Name}{finish} (now {qty}). Library: {_db.CollectionTotalCards()} cards."
            : $"Added {card.Name}{finish} to library. Library: {_db.CollectionTotalCards()} cards.";
    }

    private void AdjustSelected(int delta)
    {
        var c = SelectedCard;
        if (c == null) return;
        int newQty = c.Quantity + delta;
        if (newQty <= 0)
        {
            _db.DeleteRow(c.Id);
            Collection.Remove(c);
            UpdateSummary();
        }
        else
        {
            c.Quantity = newQty; // OnSelectedCardEdited persists and updates the summary
        }
    }

    private void DeleteSelected()
    {
        var c = SelectedCard;
        if (c == null) return;
        _db.DeleteRow(c.Id);
        Collection.Remove(c);
        UpdateSummary();
    }

    private bool FilterRow(object obj)
    {
        if (string.IsNullOrWhiteSpace(FilterText)) return true;
        if (obj is not ScannedCard c) return false;
        string q = FilterText.Trim();
        return (c.Name?.Contains(q, StringComparison.OrdinalIgnoreCase) ?? false)
            || (c.SetCode?.Contains(q, StringComparison.OrdinalIgnoreCase) ?? false)
            || (c.SetName?.Contains(q, StringComparison.OrdinalIgnoreCase) ?? false)
            || (c.TypeLine?.Contains(q, StringComparison.OrdinalIgnoreCase) ?? false);
    }

    private void LoadCollection()
    {
        Collection.Clear();
        foreach (var c in _db.GetCollection()) Collection.Add(c);
        UpdateSummary();
    }

    private void UpdateSummary()
    {
        decimal value = Collection.Sum(c => (c.EffectivePrice ?? 0m) * c.Quantity);
        CollectionSummary = $"{Collection.Count} unique · {_db.CollectionTotalCards()} total · ${value:0.00}";
    }

    // ---------------- Manual search ----------------
    private async Task SearchAsync()
    {
        var q = SearchQuery.Trim();
        if (q.Length == 0) return;
        IsSearching = true;
        Status = $"Searching Scryfall for \"{q}\"…";
        List<ScannedCard> results;
        try { results = await _scryfall.SearchAsync(q); }
        catch (Exception ex) { Status = "Search failed: " + ex.Message; IsSearching = false; return; }

        SearchResults.Clear();
        foreach (var r in results.Take(50)) SearchResults.Add(r);
        IsSearching = false;
        Status = SearchResults.Count == 0
            ? $"No cards found for \"{q}\"."
            : $"{SearchResults.Count} result(s). Select one to load it, then Add.";
    }

    // ---------------- Export ----------------
    private List<ScannedCard> ExportItems()
        => _collectionView.Cast<ScannedCard>().ToList(); // respects the current filter + sort order

    private void CopyExport()
    {
        var items = ExportItems();
        if (items.Count == 0) { Status = "Nothing to export (library is empty or filtered out)."; return; }
        string text = CollectionExporter.Export(items, SelectedExportOption.Format);
        try { Clipboard.SetText(text); Status = $"Copied {items.Count} rows to clipboard ({SelectedExportOption.Label})."; }
        catch (Exception ex) { Status = "Copy failed: " + ex.Message; }
    }

    private void ExportToFile()
    {
        var items = ExportItems();
        if (items.Count == 0) { Status = "Nothing to export (library is empty or filtered out)."; return; }

        var (ext, suggested) = CollectionExporter.FileInfoFor(SelectedExportOption.Format);
        var dlg = new SaveFileDialog
        {
            FileName = suggested,
            DefaultExt = ext,
            Filter = ext == ".csv" ? "CSV file (*.csv)|*.csv|All files (*.*)|*.*"
                                   : "Text file (*.txt)|*.txt|All files (*.*)|*.*"
        };
        if (dlg.ShowDialog() != true) return;

        try
        {
            File.WriteAllText(dlg.FileName, CollectionExporter.Export(items, SelectedExportOption.Format));
            Status = $"Exported {items.Count} rows to {dlg.FileName} ({SelectedExportOption.Label}).";
        }
        catch (Exception ex) { Status = "Export failed: " + ex.Message; }
    }

    // ---------------- Index build ----------------
    private async Task BuildIndexAsync(string bulkType)
    {
        if (IsBuildingIndex) return;
        IsBuildingIndex = true;
        _buildCts = new CancellationTokenSource();
        var progress = new Progress<IndexProgress>(p =>
        {
            BuildProgress = p.Message ?? $"Processed {p.Processed:n0} · added {p.Added:n0} · skipped {p.Skipped:n0}"
                            + (p.CurrentName != null ? $"  ({p.CurrentName})" : "");
            if (p.Done) UpdateIndexStatus();
        });

        try
        {
            await _indexBuilder.BuildAsync(bulkType, progress, _buildCts.Token);
            _matcher.ReloadIndex();
            _db.SetMeta("last_index_sync", DateTimeOffset.Now.ToString("o"));
            UpdateIndexStatus();
        }
        catch (OperationCanceledException)
        {
            _matcher.ReloadIndex();
            UpdateIndexStatus();
            BuildProgress = "Index build cancelled (progress saved — re-run to resume).";
        }
        catch (Exception ex)
        {
            BuildProgress = "Index build failed: " + ex.Message;
        }
        finally
        {
            IsBuildingIndex = false;
            _buildCts?.Dispose();
            _buildCts = null;
        }
    }

    private void UpdateIndexStatus()
    {
        int n = _db.IndexCount();
        IndexStatus = n == 0
            ? "Image index: empty (OCR-only matching). It will build automatically, or press Build."
            : $"Image index: {n:n0} cards.";
    }

    /// <summary>
    /// On launch, kick off a background full-index build/update when the index is empty or
    /// stale. It downloads the bulk list to find what's missing, then hashes only new or
    /// not-yet-art-hashed cards (existing entries are skipped), so subsequent syncs are cheap.
    /// </summary>
    private void MaybeAutoUpdateIndex()
    {
        if (!AutoUpdateIndex || IsBuildingIndex) return;

        bool empty = _db.IndexCount() == 0;
        bool stale = true;
        if (DateTimeOffset.TryParse(_db.GetMeta("last_index_sync"), out var last))
            stale = DateTimeOffset.Now - last > IndexRefreshInterval;

        if (empty || stale)
        {
            Status = empty
                ? "Building the image index for the first time (downloads in the background — you can scan meanwhile)."
                : "Checking Scryfall for new cards to add to the image index…";
            _ = BuildIndexAsync("default_cards");
        }
    }

    private async Task RefreshDevicesAsync()
    {
        Status = "Detecting video devices…";
        var found = await Task.Run(() => CameraService.EnumerateDevices());
        int? keepIndex = SelectedDevice?.Index;
        Devices.Clear();
        foreach (var d in found) Devices.Add(d);
        // Preserve the current selection across a refresh where possible, else pick the first.
        SelectedDevice = Devices.FirstOrDefault(d => d.Index == keepIndex) ?? Devices.FirstOrDefault();
        Status = Devices.Count > 0
            ? $"Found {Devices.Count} device(s). Select one and press Start."
            : "No video devices found. Connect a camera and press Refresh.";
    }

    private void OpenScryfall()
    {
        var uri = _currentCard?.ScryfallUri;
        if (string.IsNullOrEmpty(uri)) return;
        try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(uri) { UseShellExecute = true }); }
        catch { /* ignore */ }
    }

    private static ImageSource? LoadRemoteImage(string? url)
    {
        if (string.IsNullOrEmpty(url)) return null;
        try
        {
            var bmp = new BitmapImage();
            bmp.BeginInit();
            bmp.CacheOption = BitmapCacheOption.OnLoad;
            bmp.UriSource = new Uri(url);
            bmp.EndInit();
            return bmp;
        }
        catch { return null; }
    }

    public void Dispose()
    {
        _camera.Dispose();
        _scryfall.Dispose();
    }
}

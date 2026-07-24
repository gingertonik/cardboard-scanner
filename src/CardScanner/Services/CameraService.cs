using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using DirectShowLib;
using OpenCvSharp;

namespace CardScanner.Services;

/// <summary>A selectable video input: its OpenCV device index and friendly name.</summary>
public sealed record CameraDevice(int Index, string Name)
{
    public override string ToString() => Name;
}

/// <summary>
/// Captures frames from a connected video device on a background thread and raises
/// <see cref="FrameReady"/> for each frame. Subscribers own the Mat they receive and
/// must dispose it (it is a per-frame clone).
/// </summary>
public sealed class CameraService : IDisposable
{
    private VideoCapture? _capture;
    private CancellationTokenSource? _cts;
    private Task? _loop;

    public int DeviceIndex { get; private set; }
    public bool IsRunning => _loop is { IsCompleted: false };
    public int FrameWidth { get; private set; }
    public int FrameHeight { get; private set; }

    /// <summary>Raised per frame with a BGR Mat clone the handler must dispose.</summary>
    public event EventHandler<Mat>? FrameReady;
    /// <summary>Raised if the device cannot be opened or the stream ends unexpectedly.</summary>
    public event EventHandler<string>? Error;

    /// <summary>Human-readable reason the last <see cref="Start"/> failed (null on success).</summary>
    public string? LastError { get; private set; }

    private const string DeviceBusyMessage =
        "This video device can't be opened — it's most likely already in use by another " +
        "application (Discord, Zoom, OBS, Teams, the Camera app, etc.).\n\n" +
        "Close the other app that's using the camera, or pick a different device, then try again.";

    /// <summary>
    /// Try to start capturing from a device. Returns false (setting <see cref="LastError"/>)
    /// rather than throwing when the device is missing or in use, so the caller can prompt
    /// the user instead of crashing.
    /// </summary>
    public bool Start(int deviceIndex, int requestedWidth = 1280, int requestedHeight = 720)
    {
        Stop();
        LastError = null;
        DeviceIndex = deviceIndex;

        VideoCapture? capture = null;
        try
        {
            // DShow tends to be the most reliable backend for USB webcams on Windows.
            capture = TryOpen(deviceIndex, VideoCaptureAPIs.DSHOW)
                      ?? TryOpen(deviceIndex, VideoCaptureAPIs.ANY);

            if (capture == null)
            {
                LastError = DeviceBusyMessage;
                return false;
            }

            capture.Set(VideoCaptureProperties.FrameWidth, requestedWidth);
            capture.Set(VideoCaptureProperties.FrameHeight, requestedHeight);

            // A busy device often "opens" but never delivers a frame — confirm we can grab one.
            if (!CanGrabFrame(capture))
            {
                capture.Release();
                capture.Dispose();
                LastError = DeviceBusyMessage;
                return false;
            }

            FrameWidth = (int)capture.Get(VideoCaptureProperties.FrameWidth);
            FrameHeight = (int)capture.Get(VideoCaptureProperties.FrameHeight);

            _capture = capture;
            _cts = new CancellationTokenSource();
            var token = _cts.Token;
            _loop = Task.Run(() => CaptureLoop(token), token);
            return true;
        }
        catch (Exception ex)
        {
            try { capture?.Release(); capture?.Dispose(); } catch { }
            LastError = DeviceBusyMessage + "\n\n(Details: " + ex.Message + ")";
            return false;
        }
    }

    private static VideoCapture? TryOpen(int index, VideoCaptureAPIs api)
    {
        try
        {
            var cap = new VideoCapture(index, api);
            if (cap.IsOpened()) return cap;
            cap.Dispose();
        }
        catch { /* backend threw — treat as unavailable */ }
        return null;
    }

    /// <summary>Attempt to read one frame within ~1s to prove the device is actually usable.</summary>
    private static bool CanGrabFrame(VideoCapture capture)
    {
        using var probe = new Mat();
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try
            {
                if (capture.Read(probe) && !probe.Empty()) return true;
            }
            catch { return false; }
            Thread.Sleep(50);
        }
        return false;
    }

    private void CaptureLoop(CancellationToken token)
    {
        using var frame = new Mat();
        int consecutiveFailures = 0;
        while (!token.IsCancellationRequested && _capture != null)
        {
            bool ok;
            try { ok = _capture.Read(frame); }
            catch { ok = false; }

            if (!ok || frame.Empty())
            {
                if (++consecutiveFailures > 30)
                {
                    Error?.Invoke(this, "Video stream ended or device was disconnected.");
                    break;
                }
                Thread.Sleep(15);
                continue;
            }
            consecutiveFailures = 0;

            // Hand the subscriber its own copy so our reusable buffer can be overwritten.
            FrameReady?.Invoke(this, frame.Clone());

            Thread.Sleep(10); // ~ up to 60-100 fps ceiling; real rate is device-limited
        }
    }

    public void Stop()
    {
        try
        {
            _cts?.Cancel();
            _loop?.Wait(1000);
        }
        catch { /* ignore shutdown races */ }
        finally
        {
            _capture?.Release();
            _capture?.Dispose();
            _capture = null;
            _cts?.Dispose();
            _cts = null;
            _loop = null;
        }
    }

    /// <summary>
    /// List connected video input devices with friendly names. Uses DirectShow enumeration,
    /// whose ordering matches OpenCV's DSHOW device index, so <see cref="CameraDevice.Index"/>
    /// can be passed straight to <see cref="Start"/>. Falls back to probing indices by opening
    /// them if name enumeration is unavailable.
    /// </summary>
    public static List<CameraDevice> EnumerateDevices(int maxProbe = 8)
    {
        var found = new List<CameraDevice>();
        try
        {
            var devices = DsDevice.GetDevicesOfCat(FilterCategory.VideoInputDevice);
            try
            {
                for (int i = 0; i < devices.Length; i++)
                {
                    string name = string.IsNullOrWhiteSpace(devices[i].Name) ? $"Camera {i}" : devices[i].Name;
                    found.Add(new CameraDevice(i, name));
                }
            }
            finally
            {
                foreach (var d in devices) d.Dispose();
            }
        }
        catch { /* DirectShow enumeration unavailable — fall back below */ }

        if (found.Count == 0)
        {
            for (int i = 0; i < maxProbe; i++)
            {
                try
                {
                    using var cap = new VideoCapture(i, VideoCaptureAPIs.DSHOW);
                    if (cap.IsOpened()) found.Add(new CameraDevice(i, $"Camera {i}"));
                }
                catch { /* skip */ }
            }
        }
        return found;
    }

    public void Dispose() => Stop();
}

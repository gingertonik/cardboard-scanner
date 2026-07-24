using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;

namespace CardScanner;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Safety net: surface unexpected errors as a dialog instead of crashing the app.
        DispatcherUnhandledException += OnDispatcherException;
        TaskScheduler.UnobservedTaskException += (_, args) => args.SetObserved();

        // Headless self-test: exercises the non-UI pipeline (DB, hashing, detection,
        // Scryfall lookup) without opening a window. Run with:  CardScanner.exe --selftest
        if (e.Args.Contains("--selftest"))
        {
            // Run off the dispatcher thread: blocking the STA thread while awaiting
            // continuations that resume on it would deadlock.
            int code = Task.Run(() => SelfTest.RunAsync(e.Args)).GetAwaiter().GetResult();
            Shutdown(code);
            return;
        }

        var window = new MainWindow();
        window.Show();
    }

    private void OnDispatcherException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        MessageBox.Show(
            "Something went wrong:\n\n" + e.Exception.Message +
            "\n\nThe app will keep running. If this persists, restart it.",
            "Unexpected error", MessageBoxButton.OK, MessageBoxImage.Warning);
        e.Handled = true; // keep the app alive
    }
}

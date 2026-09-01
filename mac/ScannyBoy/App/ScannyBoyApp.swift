import SwiftUI

@main
struct ScannyBoyApp: App {
    /// Shared with the `Settings` scene below, which needs the same
    /// `RollLibrary` — created once `RootView` resolves the CLI helper.
    @State private var library: RollLibrary?

    var body: some Scene {
        WindowGroup {
            RootView(library: $library)
        }
        .commands {
            CommandGroup(after: .newItem) {
                Button("Re-stitch…") {
                    NotificationCenter.default.post(name: .scannyBoyRequestRestitch, object: nil)
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }

        // Section 3.1: the library base is relocatable through a Settings
        // window.
        Settings {
            if let library {
                SettingsView(library: library)
            } else {
                Text("Scanny Boy's CLI helper is unavailable.")
                    .padding(40)
            }
        }
    }
}

extension Notification.Name {
    /// Chunk P2-10's menu command for re-stitch. A notification, rather than
    /// a focused binding, because this is a single-window app: there is only
    /// ever one `ContentView` that could possibly want to hear it.
    static let scannyBoyRequestRestitch = Notification.Name(
        "com.lonniesmith.scanny-boy.requestRestitch"
    )
}

/// Resolves the CLI helper exactly once and shows either the configuration
/// UI or, if the helper cannot be found at all, why not — rather than
/// crashing at launch over a Debug-only override mistake or a helper that
/// was never staged.
struct RootView: View {
    @Binding var library: RollLibrary?
    @State private var model: ConfigurationModel?
    @State private var edit: EditModel?
    @State private var run: RunModel?
    @State private var export: ExportModel?
    @State private var unavailableReason: String?

    var body: some View {
        Group {
            if let library, let model, let edit, let run, let export {
                ContentView(library: library, model: model, edit: edit, run: run, export: export)
            } else if let unavailableReason {
                HelperUnavailableView(reason: unavailableReason)
            } else {
                ProgressView()
            }
        }
        .onAppear(perform: resolveRunnerIfNeeded)
    }

    private func resolveRunnerIfNeeded() {
        guard library == nil, unavailableReason == nil else { return }
        do {
            // One resolved helper, shared by the probes, the conversion, and
            // the library.
            let runner = try CLIRunner(locator: .mainBundle())
            library = RollLibrary(runner: runner, libraryBase: Self.debugLibraryBaseOverride())
            model = ConfigurationModel(runner: runner)
            edit = EditModel(runner: runner)
            run = RunModel(runner: runner)
            export = ExportModel(runner: runner)
        } catch let error as CLILocatorError {
            unavailableReason = error.description
        } catch {
            unavailableReason = error.localizedDescription
        }
    }

    /// Section 4: "Never test the library against the real `~/Pictures`."
    /// `CLILocator`'s `SCANNY_BOY_CLI` override is the precedent for this —
    /// a Debug-only, absolute-path environment override so
    /// `ScannyBoyUITests` can point the real running app at a temporary
    /// library base instead of `RollLibrary`'s own `.picturesDirectory`
    /// default. Release builds never honour it.
    static let libraryBaseOverrideEnvironmentKey = "SCANNY_BOY_LIBRARY_BASE"

    private static func debugLibraryBaseOverride() -> URL? {
        #if DEBUG
        guard let override = ProcessInfo.processInfo.environment[libraryBaseOverrideEnvironmentKey],
            !override.isEmpty
        else { return nil }
        return URL(filePath: override, directoryHint: .isDirectory)
        #else
        return nil
        #endif
    }
}

struct HelperUnavailableView: View {
    let reason: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle)
            Text("Scanny Boy's CLI helper is unavailable")
                .font(.headline)
            Text(reason)
                .font(.body)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
        }
        .padding(40)
        .frame(minWidth: 420, minHeight: 200)
    }
}

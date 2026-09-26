import SwiftUI

@main
struct ScannyBoyApp: App {
    /// Shared with the `Settings` scene below, which needs the same
    /// `RollLibrary` — created once `resolveRunnerIfNeeded` resolves the CLI
    /// helper.
    @State private var library: RollLibrary?
    /// Shared with the Add Scans stage's profile picker, same as `library`.
    @State private var rig: RigModel?
    @State private var grid: GridModel?
    @State private var model: ConfigurationModel?
    @State private var capture: CaptureSessionModel?
    @State private var stitchQueue: StitchQueueModel?
    @State private var edit: EditModel?
    @State private var run: RunModel?
    @State private var export: ExportModel?
    @State private var activity: AppActivity?
    @State private var unavailableReason: String?
    @State private var keyboard = AppKeyboardState()
    /// Set at the very top of `resolveRunnerIfNeeded`, before anything else
    /// runs. Lives here rather than on `RootView` because `.onAppear` can
    /// fire for more than one `RootView` instance — observed alongside
    /// SwiftUI's own "List with selection ... tried to update multiple times
    /// per frame" diagnostic on the roll sidebar, and consistent with macOS
    /// window-state restoration briefly standing up a second window at
    /// launch. A guard stored on `RootView`'s own `@State` cannot see across
    /// two such instances and lets each build its own full model family,
    /// including its own `CLIRunner` and so its own resident
    /// `scanny-boy serve` child; the loser then leaks, unstopped, while the
    /// CLI's single-worker-at-a-time daemon on the *other* runner queues the
    /// Edit tab's requests — the 100% zoom's `render-region` call included —
    /// behind whatever landed there first. `App` conformers have exactly one
    /// persistent `@State` identity for the process's whole run, unlike a
    /// `WindowGroup`'s content view, so a flag stored here is proof against
    /// that race regardless of how many window instances appear.
    @State private var didResolveRunner = false

    /// Drops the render caches written before `PreviewCache` scoped them by
    /// roll. They are loose in `~/Library/Caches` and name no roll anywhere
    /// in their paths, so nothing can ever match them against a live roll —
    /// they would otherwise grow forever. Detached because the set can be
    /// large after a long run of the old layout, and nothing waits on it:
    /// these are caches, and re-rendering one is a round trip.
    init() {
        AppEnvironment.isolateForTestsIfNeeded()
        let cache = PreviewCache.shared
        Task.detached(priority: .utility) {
            cache.purgeUnscopedCaches()
        }
        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification,
            object: nil,
            queue: .main
        ) { notification in
            guard let closingWindow = notification.object as? NSWindow else { return }
            DispatchQueue.main.async {
                // Only the primary window should end the process. SwiftUI
                // pickers (.menu), sheets, and other transient windows also
                // post willClose; counting visible windows catches those —
                // dismissing a crop-ratio menu leaves one visible window and
                // looked like "close the last window".
                guard closingWindow.isMainWindow else { return }
                NSApplication.shared.terminate(nil)
            }
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView(
                library: library,
                rig: rig,
                grid: grid,
                model: model,
                capture: capture,
                stitchQueue: stitchQueue,
                edit: edit,
                run: run,
                export: export,
                activity: activity,
                unavailableReason: unavailableReason,
                keyboard: keyboard
            )
            .onAppear(perform: resolveRunnerIfNeeded)
        }
        .commands {
            CommandGroup(after: .newItem) {
                Button("Re-stitch…") {
                    NotificationCenter.default.post(name: .scannyBoyRequestRestitch, object: nil)
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                Button("Scanning Rig Profiles…") {
                    NotificationCenter.default.post(
                        name: .scannyBoyRequestFlatFieldProfiles, object: nil
                    )
                }
                Button("Grid Configurations…") {
                    NotificationCenter.default.post(
                        name: .scannyBoyRequestGridProfiles, object: nil
                    )
                }
            }
            AppKeyboardCommands(keyboard: keyboard)
        }

        // Section 3.1: the library base is relocatable through a Settings
        // window.
        Settings {
            if let library, let capture {
                SettingsView(library: library, capture: capture)
            } else {
                Text("Scanny Boy's CLI helper is unavailable.")
                    .padding(40)
            }
        }
    }

    private func resolveRunnerIfNeeded() {
        guard !didResolveRunner else { return }
        didResolveRunner = true
        do {
            // One resolved helper, shared by the probes, the conversion, and
            // the library.
            let runner = try CLIRunner(locator: .mainBundle())
            library = RollLibrary(runner: runner, libraryBase: Self.debugLibraryBaseOverride())
            let rig = RigModel(runner: runner)
            let grid = GridModel(runner: runner)
            let edit = EditModel(runner: runner)
            let run = RunModel(runner: runner)
            let export = ExportModel(runner: runner)
            let configuration = ConfigurationModel(runner: runner)
            let camera = TetherCamera()
            let capture = CaptureSessionModel(runner: runner, camera: camera)
            let stitchQueue = StitchQueueModel(runner: runner)
            self.rig = rig
            self.grid = grid
            model = configuration
            self.capture = capture
            self.stitchQueue = stitchQueue
            self.edit = edit
            self.run = run
            self.export = export
            activity = AppActivity(
                run: run, edit: edit, export: export, rig: rig,
                configuration: configuration, capture: capture, stitchQueue: stitchQueue
            )
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

extension Notification.Name {
    /// Chunk P2-10's menu command for re-stitch. A notification, rather than
    /// a focused binding, because this is a single-window app: there is only
    /// ever one `ContentView` that could possibly want to hear it.
    static let scannyBoyRequestRestitch = Notification.Name(
        "com.lonniesmith.scanny-boy.requestRestitch"
    )
    /// The flat-field profile manager, for the same reason.
    static let scannyBoyRequestFlatFieldProfiles = Notification.Name(
        "com.lonniesmith.scanny-boy.requestFlatFieldProfiles"
    )
    static let scannyBoyRequestGridProfiles = Notification.Name(
        "com.lonniesmith.scanny-boy.requestGridProfiles"
    )
}

/// Shows the configured app once `ScannyBoyApp.resolveRunnerIfNeeded` has
/// resolved the CLI helper, the helper-unavailable message if it could not
/// be found, or a spinner while that resolution is still in flight. Purely
/// a function of what it's handed: the resolution itself, and the one-shot
/// guard around it, live on `ScannyBoyApp` — the only `@State` identity
/// guaranteed not to multiply if SwiftUI stands up more than one window.
struct RootView: View {
    let library: RollLibrary?
    let rig: RigModel?
    let grid: GridModel?
    let model: ConfigurationModel?
    let capture: CaptureSessionModel?
    let stitchQueue: StitchQueueModel?
    let edit: EditModel?
    let run: RunModel?
    let export: ExportModel?
    let activity: AppActivity?
    let unavailableReason: String?
    let keyboard: AppKeyboardState

    var body: some View {
        if let library, let rig, let grid, let model, let capture, let stitchQueue,
           let edit, let run, let export, let activity
        {
            ContentView(
                library: library,
                rig: rig,
                grid: grid,
                model: model,
                capture: capture,
                stitchQueue: stitchQueue,
                edit: edit,
                run: run,
                export: export,
                activity: activity,
                keyboard: keyboard
            )
        } else if let unavailableReason {
            HelperUnavailableView(reason: unavailableReason)
        } else {
            ProgressView()
        }
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

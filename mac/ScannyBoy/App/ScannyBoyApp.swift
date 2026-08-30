import SwiftUI

@main
struct ScannyBoyApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
        }
        .commands {
            CommandGroup(after: .newItem) {
                Button("Re-stitch…") {
                    NotificationCenter.default.post(name: .scannyBoyRequestRestitch, object: nil)
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
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
    @State private var model: ConfigurationModel?
    @State private var run: RunModel?
    @State private var unavailableReason: String?

    var body: some View {
        Group {
            if let model, let run {
                ContentView(model: model, run: run)
            } else if let unavailableReason {
                HelperUnavailableView(reason: unavailableReason)
            } else {
                ProgressView()
            }
        }
        .onAppear(perform: resolveRunnerIfNeeded)
    }

    private func resolveRunnerIfNeeded() {
        guard model == nil, unavailableReason == nil else { return }
        do {
            // One resolved helper, shared by the probes and the conversion.
            let runner = try CLIRunner(locator: .mainBundle())
            model = ConfigurationModel(runner: runner)
            run = RunModel(runner: runner)
        } catch let error as CLILocatorError {
            unavailableReason = error.description
        } catch {
            unavailableReason = error.localizedDescription
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

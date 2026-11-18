import Foundation
import Observation

/// The Settings pane's Frame log section: the base URL, the saved token
/// (shown only by its prefix), and a test connection run through the CLI.
/// The token reaches the CLI in its environment, never in argv.
@MainActor
@Observable
final class FrameLogConnectionModel {
    enum TestState: Sendable, Equatable {
        case idle
        case testing
        case connected(rolls: Int)
        case failed(String)
    }

    let runner: CLIRunner
    /// Bound to the Base URL field; written to settings by `commitBaseURL`.
    var baseURL: String
    /// Bound to the token field; emptied once a token is saved.
    var tokenDraft = ""
    private(set) var tokenPrefix: String?
    private(set) var testState: TestState = .idle
    private(set) var errorMessage: String?

    private let tokens: any FrameLogTokenStore
    private let settings: FrameLogSettings

    init(
        runner: CLIRunner,
        tokens: any FrameLogTokenStore = AppEnvironment.frameLogTokenStore,
        settings: FrameLogSettings = FrameLogSettings()
    ) {
        self.runner = runner
        self.tokens = tokens
        self.settings = settings
        baseURL = settings.baseURL
        tokenPrefix = tokens.tokenPrefix
    }

    /// Writes the Base URL field through to the settings. Called on submit
    /// and before a test, so the test uses what the user typed. A blank
    /// field falls back to the default, and the field shows that.
    func commitBaseURL() {
        settings.baseURL = baseURL
        baseURL = settings.baseURL
    }

    func saveToken() {
        let token = tokenDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return }
        do {
            try tokens.setToken(token)
            tokenDraft = ""
            tokenPrefix = tokens.tokenPrefix
            testState = .idle
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func removeToken() {
        do {
            try tokens.clear()
            tokenPrefix = nil
            testState = .idle
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Runs `framelog rolls --status active` and reports the roll count, or
    /// the error the CLI gave.
    func testConnection() async {
        commitBaseURL()
        guard let token = tokens.token() else {
            testState = .failed("Save a token first.")
            return
        }
        testState = .testing
        let command = CLICommand
            .framelogRolls(status: "active", baseURL: settings.baseURL)
            .withEnvironment([FrameLogCredentials.tokenEnvironmentKey: token])
        testState = await Self.runTest(runner: runner, command: command)
    }

    private static func runTest(runner: CLIRunner, command: CLICommand) async -> TestState {
        var result: TestState?
        var outcome: CLIOutcome?
        do {
            for await output in try await runner.session(for: command).start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .framelogRolls:
                        result = .connected(rolls: event.frameLogRolls?.count ?? 0)
                    case .error:
                        result = .failed(event.message ?? "The Frame log request failed.")
                    default:
                        continue
                    }
                case .completed(let completion):
                    outcome = completion.outcome
                case .log, .failure:
                    continue
                }
            }
        } catch {
            return .failed(error.localizedDescription)
        }
        let finalResult = result ?? .failed("The Frame log request produced no result.")
        if let outcome, outcome != .success, case .connected = finalResult {
            return .failed("The Frame log request did not complete successfully.")
        }
        return finalResult
    }
}

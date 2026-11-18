import Foundation
import Testing

@testable import ScannyBoy

/// The token store, the settings and the Settings pane's model. Nothing here
/// touches the Keychain: the tests run in the `.test` profile, and the
/// model is given an in-memory store.
@Suite("Frame log credentials and settings")
@MainActor
struct FrameLogCredentialsTests {
    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func isolatedDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    private static let token = "fl_abcdefgh0123456789"

    @Test("the test profile keeps its token in memory, not the Keychain")
    func testProfileStoreIsInMemory() {
        #expect(AppEnvironment.frameLogTokenStore is InMemoryFrameLogTokenStore)
    }

    @Test("an in-memory store saves, shows a prefix and clears")
    func inMemoryStoreRoundTrip() throws {
        let store = InMemoryFrameLogTokenStore()
        #expect(store.token() == nil)
        #expect(store.tokenPrefix == nil)

        try store.setToken(Self.token)
        #expect(store.token() == Self.token)
        #expect(store.tokenPrefix == "fl_abcdefgh")

        try store.clear()
        #expect(store.token() == nil)
    }

    @Test("the display prefix is at most fl_ plus eight characters")
    func tokenPrefixLength() {
        let store = InMemoryFrameLogTokenStore(token: Self.token)
        #expect(store.tokenPrefix?.count == 11)
    }

    @Test("the base URL defaults to the Frame log site")
    func baseURLDefault() {
        let settings = FrameLogSettings(defaults: Self.isolatedDefaults())
        #expect(settings.baseURL == "https://lonnie-smith.com")
    }

    @Test("a base URL is stored under its key, trimmed, and a blank one restores the default")
    func baseURLStoresAndResets() {
        let defaults = Self.isolatedDefaults()
        let settings = FrameLogSettings(defaults: defaults)

        settings.baseURL = "  https://frames.example.test  "
        #expect(settings.baseURL == "https://frames.example.test")
        #expect(defaults.string(forKey: "framelogBaseURL") == "https://frames.example.test")

        settings.baseURL = "   "
        #expect(settings.baseURL == "https://lonnie-smith.com")
        #expect(defaults.string(forKey: "framelogBaseURL") == nil)
    }

    @Test("the model reports no connection without a token")
    func testConnectionNeedsToken() async {
        let model = FrameLogConnectionModel(
            runner: CLIRunner(executable: URL(filePath: "/usr/bin/false")),
            tokens: InMemoryFrameLogTokenStore(),
            settings: FrameLogSettings(defaults: Self.isolatedDefaults())
        )
        await model.testConnection()
        #expect(model.testState == .failed("Save a token first."))
    }

    @Test("saving a token shows its prefix, clears the draft, and removing it hides it")
    func saveAndRemoveToken() {
        let store = InMemoryFrameLogTokenStore()
        let model = FrameLogConnectionModel(
            runner: CLIRunner(executable: URL(filePath: "/usr/bin/false")),
            tokens: store,
            settings: FrameLogSettings(defaults: Self.isolatedDefaults())
        )
        #expect(model.tokenPrefix == nil)

        model.tokenDraft = "  \(Self.token)  "
        model.saveToken()
        #expect(store.token() == Self.token)
        #expect(model.tokenPrefix == "fl_abcdefgh")
        #expect(model.tokenDraft.isEmpty)

        model.removeToken()
        #expect(store.token() == nil)
        #expect(model.tokenPrefix == nil)
    }

    @Test("test connection reports the roll count the CLI returns")
    func testConnectionCountsRolls() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let executable = try TestSupport.writeTestExecutable(
            #"""
            if [ "$SCANNY_BOY_FRAMELOG_TOKEN" = "\#(Self.token)" ]; then
              echo '{"protocol_version":29,"event":"framelog_rolls","rolls":[{"uid":"abcde12345","roll_number":1},{"uid":"fghij67890","roll_number":2}]}'
            fi
            echo '{"protocol_version":29,"event":"finished","status":"success","exit_status":0}'
            """#,
            in: directory
        )
        let model = FrameLogConnectionModel(
            runner: CLIRunner(executable: executable),
            tokens: InMemoryFrameLogTokenStore(token: Self.token),
            settings: FrameLogSettings(defaults: Self.isolatedDefaults())
        )
        await model.testConnection()
        #expect(model.testState == .connected(rolls: 2))
    }

    @Test("test connection shows the CLI's error message")
    func testConnectionShowsError() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let executable = try TestSupport.writeTestExecutable(
            #"""
            echo '{"protocol_version":29,"event":"error","code":"FRAMELOG_UNAUTHORIZED","message":"bad token"}'
            echo '{"protocol_version":29,"event":"finished","status":"failed","exit_status":1}'
            """#,
            in: directory
        )
        let model = FrameLogConnectionModel(
            runner: CLIRunner(executable: executable),
            tokens: InMemoryFrameLogTokenStore(token: Self.token),
            settings: FrameLogSettings(defaults: Self.isolatedDefaults())
        )
        await model.testConnection()
        #expect(model.testState == .failed("bad token"))
    }
}

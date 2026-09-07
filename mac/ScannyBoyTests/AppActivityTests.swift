import Foundation
import Testing

@testable import ScannyBoy

/// Verifies `AppActivity.isBusy` reflects every helper the Add Scans stage
/// can start, not only `RunModel`'s conversion session.
@Suite("AppActivity")
@MainActor
struct AppActivityTests {
    private static func isolatedDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static let started = TestEvents.line(#"{"event":"started","command":"probe"}"#)
    private static let finishedSuccess =
        TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#)

    private static func rollInfoEvent(filmBaseJSON: String, filmKind: String? = "colour") -> String {
        let filmKindJSON = filmKind.map { "\"\($0)\"" } ?? "null"
        let manifest = """
        {"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","runs":[],"negatives":[],"metadata":{},"film_kind":\(filmKindJSON),"film_base":\(filmBaseJSON)}
        """
        return TestEvents.line(#"{"event":"roll_info","manifest":\#(manifest)}"#)
    }

    /// A fake helper that sleeps on the commands that drive ConfigurationModel
    /// busy flags so tests can observe `AppActivity.isBusy` mid-flight.
    private static func slowConfigurationExecutable(
        in directory: URL,
        sleepSeconds: Double = 0.2
    ) throws -> URL {
        let baseMarker = directory.appending(path: ".film-base-attached").path
        let kindMarker = directory.appending(path: ".film-kind-set").path
        let attachedFilmBase = """
        {"density":[-0.42,-0.12,-0.99],"locked_at":null,"source_name":"_DSC5012.NEF","populations":[{"density":[-0.42,-0.12,-0.99],"luma":-0.25,"area_fraction":0.44,"cells":34100,"spread":0.012}]}
        """
        let script = """
            BASE_MARKER='\(baseMarker)'
            KIND_MARKER='\(kindMarker)'
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"roll info"}"#))'
              if [ -f "$BASE_MARKER" ]; then
                echo '\(rollInfoEvent(filmBaseJSON: attachedFilmBase))'
              elif [ -f "$KIND_MARKER" ]; then
                echo '\(rollInfoEvent(filmBaseJSON: "null", filmKind: "monochrome"))'
              else
                echo '\(rollInfoEvent(filmBaseJSON: "null"))'
              fi
              echo '\(finishedSuccess)'
              exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "set-base-frame" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"roll set-base-frame"}"#))'
              sleep \(sleepSeconds)
              touch "$BASE_MARKER"
              echo '\(TestEvents.line(
                #"{"event":"base_frame_set","roll_id":"roll-1","source_name":"_DSC5013.NEF","density":[-0.42,-0.12,-0.99],"area_fraction":0.44,"population_count":1,"locked":false}"#
              ))'
              echo '\(finishedSuccess)'
              exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "set-film-kind" ]; then
              echo '\(TestEvents.line(#"{"event":"started","command":"roll set-film-kind"}"#))'
              sleep \(sleepSeconds)
              touch "$KIND_MARKER"
              echo '\(finishedSuccess)'
              exit 0
            fi
            case "$*" in
              *--roll*)
                echo '\(started)'
                sleep \(sleepSeconds)
                echo '\(TestEvents.line(#"{"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"roll_overlap":[]}"#))'
                echo '\(finishedSuccess)'
                ;;
              *)
                echo '\(started)'
                echo '\(TestEvents.line(#"{"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[]}"#))'
                echo '\(finishedSuccess)'
                ;;
            esac
            """
        return try TestSupport.writeTestExecutable(script, in: directory)
    }

    private static func makeActivity(
        executable: URL
    ) -> (AppActivity, ConfigurationModel) {
        let runner = CLIRunner(executable: executable)
        let configuration = ConfigurationModel(
            runner: runner, defaults: isolatedDefaults()
        )
        let activity = AppActivity(
            run: RunModel(runner: runner),
            edit: EditModel(runner: runner),
            export: ExportModel(runner: runner),
            flatField: FlatFieldModel(runner: runner),
            configuration: configuration
        )
        return (activity, configuration)
    }

    @Test("isBusy is false when no helper is active")
    func isBusyWhenIdle() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.slowConfigurationExecutable(in: directory)
        let (activity, _) = Self.makeActivity(executable: executable)
        #expect(activity.isBusy == false)
    }

    @Test("isBusy while attaching a base frame")
    func isBusyWhileAttachingBaseFrame() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let frame = directory.appending(path: "_DSC5013.NEF")
        try Data().write(to: frame)

        let executable = try Self.slowConfigurationExecutable(in: directory)
        let (activity, configuration) = Self.makeActivity(executable: executable)
        configuration.rollURL = rollDir
        await configuration.waitForPendingProbes()

        let attachTask = Task { await configuration.attachBaseFrame(at: frame) }
        try await Task.sleep(for: .milliseconds(50))
        #expect(activity.isBusy == true)
        await attachTask.value
        #expect(activity.isBusy == false)
    }

    @Test("isBusy while setting film kind")
    func isBusyWhileSettingFilmKind() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)

        let executable = try Self.slowConfigurationExecutable(in: directory)
        let (activity, configuration) = Self.makeActivity(executable: executable)
        configuration.rollURL = rollDir
        await configuration.waitForPendingProbes()

        let setKindTask = Task { await configuration.setFilmKind("monochrome") }
        try await Task.sleep(for: .milliseconds(50))
        #expect(activity.isBusy == true)
        await setKindTask.value
        #expect(activity.isBusy == false)
    }

    @Test("isBusy while validating selection")
    func isBusyWhileValidatingSelection() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)

        let executable = try Self.slowConfigurationExecutable(in: directory)
        let (activity, configuration) = Self.makeActivity(executable: executable)
        configuration.inputFolder = directory
        await configuration.waitForPendingProbes()
        configuration.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        configuration.rollURL = rollDir
        await configuration.waitForPendingProbes()
        configuration.across = 3

        let validateTask = Task { await configuration.validateSelection() }
        try await Task.sleep(for: .milliseconds(50))
        #expect(activity.isBusy == true)
        _ = await validateTask.value
        #expect(activity.isBusy == false)
    }
}

import Foundation
import Testing

@testable import ScannyBoy

/// Drives `ConfigurationModel` against a fake CLI executable that answers
/// `probe` and `roll info` from canned JSON, matching the pattern
/// `CLISessionTests` and `CLIIntegrationTests` already use for a synthetic
/// helper. No test here touches the real sample NEFs or the bundled helper —
/// those are exercised by the CLI's own test suite and by
/// `RunIntegrationTests`.
@Suite("Configuration model (Chunk 9, reworked onto rolls by Chunk P3-11)")
@MainActor
struct ConfigurationModelTests {
    private static func isolatedDefaults() -> UserDefaults {
        // A fresh, unique suite per test: never `.standard`, and never
        // shared between concurrently running tests (section 7).
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    /// `TestSupport.withTemporaryDirectory`'s closure-passing shape doesn't
    /// fit here: this suite is `@MainActor` (it drives an `@MainActor`
    /// model), and handing a `@MainActor`-isolated closure to that helper's
    /// plain nonisolated generic parameter trips Swift 6's "sending main
    /// actor-isolated value" diagnostic. A flat create-and-`defer` pair
    /// avoids crossing an isolation boundary at all.
    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// A `probe` stand-in that answers from `$*`: `--roll` present routes to
    /// `withFilesAndRoll`, `--files` alone (no `--roll`) routes to
    /// `withFiles`, and a bare `--input` routes to `catalogueOnly` — the same
    /// three call shapes `ConfigurationModel` actually makes. `roll info`
    /// and `roll set-base-frame` / `roll set-film-kind` are distinguished by
    /// `$1 $2`.
    private static func fakeProbeExecutable(
        in directory: URL,
        catalogueOnly: [String],
        withFiles: [String] = [],
        withFilesAndRoll: [String] = [],
        rollInfoLines: [String]? = nil,
        setBaseFrameLines: [String] = [],
        setFilmKindLines: [String] = [],
        setFlatFieldReferenceLines: [String] = []
    ) throws -> URL {
        func echoLines(_ lines: [String]) -> String {
            lines.map { "echo '\($0)'" }.joined(separator: "\n")
        }
        let defaultRollInfo = [
            TestEvents.line(#"{"event":"started","command":"roll info"}"#),
            rollInfoEvent(filmBaseJSON: attachedFilmBaseJSON()),
            finishedSuccess,
        ]
        let resolvedRollInfo = rollInfoLines ?? defaultRollInfo
        let baseMarker = directory.appending(path: ".film-base-attached").path
        let ffMarker = directory.appending(path: ".flat-field-attached").path
        let kindMarker = directory.appending(path: ".film-kind-set").path
        let defaultSetFlatFieldReferenceSuccess = [
            TestEvents.line(#"{"event":"started","command":"roll set-flatfield-reference"}"#),
            TestEvents.line(
                #"{"event":"flat_field_reference_set","roll_id":"roll-1","source_name":"bare-light.dng","reference_width":6064,"reference_height":4040,"rig_profile_id":null,"locked":false}"#
            ),
            finishedSuccess,
        ]
        let defaultSetFilmKindSuccess = [
            TestEvents.line(#"{"event":"started","command":"roll set-film-kind"}"#),
            finishedSuccess,
        ]
        let script = """
            BASE_MARKER='\(baseMarker)'
            FF_MARKER='\(ffMarker)'
            KIND_MARKER='\(kindMarker)'
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
            if [ -f "$BASE_MARKER" ] && [ -f "$FF_MARKER" ]; then
            \(echoLines([
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                rollInfoEvent(
                    filmBaseJSON: attachedFilmBaseJSON(),
                    flatFieldJSON: attachedFlatFieldJSON()
                ),
                finishedSuccess,
            ]))
            elif [ -f "$BASE_MARKER" ]; then
            \(echoLines([
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                rollInfoEvent(filmBaseJSON: attachedFilmBaseJSON(), flatFieldJSON: "null"),
                finishedSuccess,
            ]))
            elif [ -f "$FF_MARKER" ]; then
            \(echoLines([
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                rollInfoEvent(filmBaseJSON: "null", flatFieldJSON: attachedFlatFieldJSON()),
                finishedSuccess,
            ]))
            elif [ -f "$KIND_MARKER" ]; then
            \(echoLines([
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                rollInfoEvent(filmBaseJSON: "null", filmKind: "monochrome"),
                finishedSuccess,
            ]))
            else
            \(echoLines(resolvedRollInfo))
            fi
                exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "set-base-frame" ]; then
            touch "$BASE_MARKER"
            \(echoLines(setBaseFrameLines.isEmpty ? defaultSetBaseFrameSuccess : setBaseFrameLines))
                exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "set-film-kind" ]; then
            touch "$KIND_MARKER"
            \(echoLines(setFilmKindLines.isEmpty ? defaultSetFilmKindSuccess : setFilmKindLines))
                exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "set-flatfield-reference" ]; then
            touch "$FF_MARKER"
            \(echoLines(setFlatFieldReferenceLines.isEmpty ? defaultSetFlatFieldReferenceSuccess : setFlatFieldReferenceLines))
                exit 0
            fi
            case "$*" in
              *--roll*)
            \(echoLines(withFilesAndRoll))
                ;;
              *--files*)
            \(echoLines(withFiles))
                ;;
              *)
            \(echoLines(catalogueOnly))
                ;;
            esac
            """
        return try TestSupport.writeTestExecutable(script, in: directory)
    }

    private static func attachedFilmBaseJSON() -> String {
        """
        {"density":[-0.42,-0.12,-0.99],"locked_at":null,"source_name":"_DSC5012.NEF","populations":[{"density":[-0.42,-0.12,-0.99],"luma":-0.25,"area_fraction":0.44,"cells":34100,"spread":0.012}]}
        """
    }

    private static func attachedFlatFieldJSON() -> String {
        """
        {"source_name":"bare-light.dng","reference_width":6064,"reference_height":4040,"rig_profile_id":null,"locked_at":null}
        """
    }

    private static func rollInfoEvent(
        filmBaseJSON: String,
        flatFieldJSON: String? = nil,
        filmKind: String? = "colour"
    ) -> String {
        let filmKindJSON = filmKind.map { "\"\($0)\"" } ?? "null"
        let flatField = flatFieldJSON ?? attachedFlatFieldJSON()
        let manifest = """
        {"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","runs":[],"negatives":[],"metadata":{},"film_kind":\(filmKindJSON),"film_base":\(filmBaseJSON),"flat_field":\(flatField)}
        """
        return TestEvents.line(#"{"event":"roll_info","manifest":\#(manifest)}"#)
    }

    private static let defaultSetBaseFrameSuccess = [
        TestEvents.line(#"{"event":"started","command":"roll set-base-frame"}"#),
        TestEvents.line(
            #"{"event":"base_frame_set","roll_id":"roll-1","source_name":"_DSC5013.NEF","density":[-0.42,-0.12,-0.99],"area_fraction":0.44,"population_count":1,"locked":false}"#
        ),
        finishedSuccess,
    ]

    private static let started = TestEvents.line(#"{"event":"started","command":"probe"}"#)
    private static let finishedSuccess =
        TestEvents.line(#"{"event":"finished","status":"success","exit_status":0}"#)
    private static let finishedFailed =
        TestEvents.line(#"{"event":"finished","status":"failed","exit_status":1}"#)

    private static func errorEvent(code: String) -> String {
        TestEvents.line(#"{"event":"error","code":"\#(code)","message":"synthetic failure"}"#)
    }

    // Deliberately not alphabetical: proves the model displays `probe`'s
    // order verbatim instead of re-sorting — Swift always uses
    // the order it is given and never sorts files itself.
    private static let catalogueUnsorted =
        TestEvents.line(#"{"event":"probe_result","catalogue":["c.NEF","a.NEF","b.NEF"],"warnings":[],"groups":[]}"#)

    private static let catalogueABC =
        TestEvents.line(#"{"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[]}"#)

    // Raw string literals do not support backslash line-continuation (that
    // is a plain-string-literal escape only), so each of these stays on one
    // line rather than risk a stray literal backslash inside the JSON.
    private static let threeFileGroupNoRoll =
        TestEvents.line(#"{"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]]}"#)

    private static let threeFileGroupNoOverlap =
        TestEvents.line(#"{"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"roll_overlap":[]}"#)

    private static let threeFileGroupWithOverlap =
        TestEvents.line(#"{"event":"probe_result","catalogue":["a.NEF","b.NEF","c.NEF"],"warnings":[],"groups":[["a.NEF","b.NEF","c.NEF"]],"roll_overlap":[{"negative_id":"r-negative-01","expected_output":"a.tif","run_id":"r","overlapping_sources":["a.NEF","b.NEF","c.NEF"],"group_index":0}]}"#)

    private static let sixFileNames = ["n1.NEF", "n2.NEF", "n3.NEF", "n4.NEF", "n5.NEF", "n6.NEF"]

    private static let sixFileTwoGroups =
        TestEvents.line(#"{"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[["n1.NEF","n2.NEF","n3.NEF"],["n4.NEF","n5.NEF","n6.NEF"]]}"#)

    // MARK: - Selection shortcuts

    @Test("Cmd-A selects all catalogue entries, Cmd-D clears the selection")
    func testSelectAllAndDeselectAll() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = ["b.NEF"]

        model.selectAll()
        #expect(model.selectedFiles == Set(["a.NEF", "b.NEF", "c.NEF"]))

        model.deselectAll()
        #expect(model.selectedFiles.isEmpty)
    }

    // MARK: - Model state follows probe results

    @Test("The catalogue reflects probe's order verbatim; nothing here re-sorts it")
    func catalogueFollowsProbeOrder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueUnsorted, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()

        #expect(model.catalogue == ["c.NEF", "a.NEF", "b.NEF"])
        #expect(model.catalogueError == nil)
    }

    // MARK: - A valid six-file, three-per-negative selection shows two groups

    @Test("A valid six-file, three-per-negative selection shows two groups")
    func sixFileSelectionShowsTwoGroups() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [
                Self.started,
                TestEvents.line(#"{"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[]}"#),
                Self.finishedSuccess,
            ],
            withFiles: [Self.started, Self.sixFileTwoGroups, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = Set(Self.sixFileNames)
        model.across = 3

        #expect(model.groups.isEmpty)
        #expect(await model.validateSelection())

        #expect(model.groups == [
            ["n1.NEF", "n2.NEF", "n3.NEF"],
            ["n4.NEF", "n5.NEF", "n6.NEF"],
        ])
        #expect(model.selectionError == nil)
    }

    // MARK: - Convert-time validation

    @Test(
        "validateSelection surfaces selection errors without blocking runEnabled",
        arguments: [
            ("NON_CONTIGUOUS_SELECTION", CLICode.nonContiguousSelection),
            ("NOT_DIVISIBLE", CLICode.notDivisible),
            ("CAPTURE_SETTINGS_DIFFER", CLICode.captureSettingsDiffer),
        ]
    )
    func validateSelectionSurfacesSelectionError(_ scenario: (code: String, expected: CLICode)) async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.errorEvent(code: scenario.code), Self.finishedFailed],
            withFilesAndRoll: [Self.started, Self.errorEvent(code: scenario.code), Self.finishedFailed]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.across = 3
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]

        #expect(model.runEnabled == true)
        #expect(await model.validateSelection() == false)
        #expect(model.selectionError?.code == scenario.expected)
        #expect(model.rollError == nil)
    }

    @Test("reloadRollLocks picks up locks written after the roll was selected")
    func reloadRollLocksPicksUpNewLocks() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let lockedMarker = directory.appending(path: ".locked").path
        let lockedBase = Self.attachedFilmBaseJSON()
            .replacingOccurrences(of: #""locked_at":null"#, with: #""locked_at":"2026-09-20T20:00:00Z""#)
        let lockedFlat = Self.attachedFlatFieldJSON()
            .replacingOccurrences(of: #""locked_at":null"#, with: #""locked_at":"2026-09-20T20:00:00Z""#)
        let lockedInfo = Self.rollInfoEvent(filmBaseJSON: lockedBase, flatFieldJSON: lockedFlat)
            .replacingOccurrences(of: #""runs":[]"#, with: #""runs":[{"run_id":"r1","status":"complete"}]"#)
        let script = """
            if [ "$1" = "roll" ] && [ "$2" = "info" ]; then
              echo '\(Self.started)'
              if [ -f '\(lockedMarker)' ]; then
                echo '\(lockedInfo)'
              else
                echo '\(Self.rollInfoEvent(filmBaseJSON: Self.attachedFilmBaseJSON()))'
              fi
              echo '\(Self.finishedSuccess)'
              exit 0
            fi
            exit 0
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        await model.reloadRollLocks()
        #expect(model.filmBase?.lockedAt == nil)
        #expect(model.flatField?.lockedAt == nil)
        #expect(model.filmKindLocked == false)

        FileManager.default.createFile(atPath: lockedMarker, contents: nil)
        await model.reloadRollLocks()
        #expect(model.filmBase?.lockedAt == "2026-09-20T20:00:00Z")
        #expect(model.flatField?.lockedAt == "2026-09-20T20:00:00Z")
        #expect(model.filmKindLocked == true)
    }

    @Test("Run is disabled until a roll is selected, a grouping and a profile are chosen")
    func runDisabledUntilRollSelected() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupNoOverlap, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]

        #expect(model.rollURL == nil)
        #expect(model.runEnabled == false)

        model.rollURL = rollDir

        // Three choices remain: film type, the batch's scans-per-negative,
        // and the roll's film-base and flat-field references.
        await model.waitForPendingProbes()
        #expect(model.selectionError == nil)
        #expect(model.rollError == nil)
        #expect(model.filmKind == "colour")
        #expect(model.filmBase != nil)
        #expect(model.flatField != nil)
        #expect(model.runEnabled == false)

        model.across = 3
        #expect(model.runEnabled == true)
    }

    @Test("runEnabled stays off until a film type is chosen")
    func runEnabledGatesOnFilmKind() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            rollInfoLines: [
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                Self.rollInfoEvent(filmBaseJSON: Self.attachedFilmBaseJSON(), filmKind: nil),
                Self.finishedSuccess,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.across = 3

        #expect(model.filmKind == nil)
        #expect(model.runEnabled == false)

        await model.setFilmKind("monochrome")
        #expect(model.filmKind == "monochrome")
        #expect(model.runEnabled == true)
    }

    @Test("runEnabled stays off until a flat-field reference is attached")
    func runEnabledGatesOnFlatFieldReference() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            rollInfoLines: [
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                Self.rollInfoEvent(
                    filmBaseJSON: Self.attachedFilmBaseJSON(),
                    flatFieldJSON: "null",
                    filmKind: "colour"
                ),
                Self.finishedSuccess,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.across = 3

        #expect(model.flatField == nil)
        #expect(model.runEnabled == false)
    }

    @Test("runEnabled stays off until a base frame is attached")
    func runEnabledGatesOnFilmBase() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            rollInfoLines: [
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                Self.rollInfoEvent(
                    filmBaseJSON: "null",
                    flatFieldJSON: Self.attachedFlatFieldJSON(),
                    filmKind: "colour"
                ),
                Self.finishedSuccess,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.across = 3

        #expect(model.filmBase == nil)
        #expect(model.runEnabled == false)
    }

    @Test("rollSetBaseFrame command shape")
    func rollSetBaseFrameCommandShape() throws {
        let roll = URL(filePath: "/tmp/roll")
        let frame = URL(filePath: "/tmp/_DSC5012.NEF")
        let command = CLICommand.rollSetBaseFrame(roll: roll, frame: frame)
        #expect(command.arguments == [
            "roll", "set-base-frame",
            "--roll", roll.path,
            "--frame", frame.path,
        ])
    }

    @Test("rollSetFlatFieldReference command shape")
    func rollSetFlatFieldReferenceCommandShape() throws {
        let roll = URL(filePath: "/tmp/roll")
        let frame = URL(filePath: "/tmp/bare-light.NEF")
        let command = CLICommand.rollSetFlatFieldReference(
            roll: roll, frame: frame, rig: "pid-1"
        )
        #expect(command.arguments == [
            "roll", "set-flatfield-reference",
            "--roll", roll.path,
            "--frame", frame.path,
            "--rig", "pid-1",
        ])
    }

    @Test("attachFlatFieldReference refreshes flatField from roll info")
    func attachFlatFieldReferenceRefreshesFlatField() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let frame = directory.appending(path: "bare-light.NEF")
        try Data().write(to: frame)

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            rollInfoLines: [
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                Self.rollInfoEvent(
                    filmBaseJSON: Self.attachedFilmBaseJSON(),
                    flatFieldJSON: "null",
                    filmKind: "colour"
                ),
                Self.finishedSuccess,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        #expect(model.flatField == nil)

        await model.attachFlatFieldReference(at: frame)
        #expect(model.flatField?.sourceName == "bare-light.dng")
        #expect(model.flatFieldReferenceError == nil)
    }

    @Test("attachBaseFrame refreshes filmBase from roll info")
    func attachBaseFrameRefreshesFilmBase() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let frame = directory.appending(path: "_DSC5013.NEF")
        try Data().write(to: frame)

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            rollInfoLines: [
                TestEvents.line(#"{"event":"started","command":"roll info"}"#),
                Self.rollInfoEvent(
                    filmBaseJSON: "null",
                    flatFieldJSON: Self.attachedFlatFieldJSON(),
                    filmKind: "colour"
                ),
                Self.finishedSuccess,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        #expect(model.filmBase == nil)

        await model.attachBaseFrame(at: frame)
        #expect(model.filmBase?.sourceName == "_DSC5012.NEF")
        #expect(model.baseFrameError == nil)
    }

    @Test("Changing grid size clears groups until validateSelection runs")
    func changingPerNegativeClearsGroups() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [
                Self.started,
                TestEvents.line(#"{"event":"probe_result","catalogue":["n1.NEF","n2.NEF","n3.NEF","n4.NEF","n5.NEF","n6.NEF"],"warnings":[],"groups":[]}"#),
                Self.finishedSuccess,
            ],
            withFiles: [Self.started, Self.sixFileTwoGroups, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.selectedFiles = Set(Self.sixFileNames)

        // No grouping chosen: no groups to preview and nothing validated.
        #expect(model.groups.isEmpty)
        #expect(model.isValidating == false)

        model.across = 3
        #expect(model.groups.isEmpty)

        #expect(await model.validateSelection())
        #expect(model.groups == [
            ["n1.NEF", "n2.NEF", "n3.NEF"],
            ["n4.NEF", "n5.NEF", "n6.NEF"],
        ])
    }

    @Test("A roll-related probe failure surfaces at validateSelection without blocking runEnabled")
    func validateSelectionSurfacesRollError() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "not-a-roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [
                Self.started, Self.errorEvent(code: "ROLL_NOT_FOUND"), Self.finishedFailed,
            ]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.across = 3
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]

        #expect(model.runEnabled == true)
        #expect(await model.validateSelection() == false)
        #expect(model.rollError?.code == .rollNotFound)
        #expect(model.selectionError == nil)
    }

    @Test("Changing selection after a failed validateSelection clears stale errors")
    func changingSelectionClearsStaleErrors() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.errorEvent(code: "NOT_DIVISIBLE"), Self.finishedFailed]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.across = 3
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]

        #expect(await model.validateSelection() == false)
        #expect(model.selectionError != nil)

        model.selectedFiles = ["a.NEF", "b.NEF"]
        #expect(model.selectionError == nil)
        #expect(model.groups.isEmpty)
    }

    // MARK: - Chunk P3-11's additions: rolls and the overlap sheet

    @Test("The run command targets --roll at the batch's chosen grouping, never --film-date or --out")
    func testRunCommandOmitsFilmDateAndOutputFolder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupNoOverlap, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.across = 3
        model.rigProfileID = "pid-1"

        let command = try #require(model.buildRunCommand())
        #expect(command.arguments.contains("--roll"))
        #expect(command.arguments.contains(rollDir.path))
        #expect(!command.arguments.contains("--film-date"))
        #expect(!command.arguments.contains("--out"))
        #expect(!command.arguments.contains("--overwrite"))
        #expect(command.arguments.contains("--per-negative"))
        let perNegativeIndex = try #require(command.arguments.firstIndex(of: "--per-negative"))
        #expect(command.arguments[perNegativeIndex + 1] == "3")
        let rigIndex = try #require(command.arguments.firstIndex(of: "--rig"))
        #expect(command.arguments[rigIndex + 1] == "pid-1")
    }

    @Test("An overlapping selection still runs, and names no --skip-sources")
    func overlappingSelectionRunsWithoutSkipSources() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupWithOverlap, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.across = 3

        // Overlapping a negative already in the roll is never a reason to
        // withhold the Run command — every group runs and supersedes
        // whatever it overlaps.
        let command = try #require(model.buildRunCommand())
        #expect(!command.arguments.contains("--skip-sources"))
    }

    // MARK: - Rig profile (per-run choice)

    @Test("Selecting a roll leaves the current rig profile choice alone")
    func selectingARollDoesNotChangeTheProfile() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess],
            withFiles: [Self.started, Self.threeFileGroupNoRoll, Self.finishedSuccess],
            withFilesAndRoll: [Self.started, Self.threeFileGroupNoOverlap, Self.finishedSuccess]
        )
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rigProfileID = "pid-mine"
        // A roll never pins the picker to whatever profile its past runs
        // used — the profile is a per-run choice, so selecting a roll must
        // not disturb it.
        model.rollURL = rollDir
        await model.waitForPendingProbes()

        #expect(model.rigProfileID == "pid-mine")
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.across = 3
        #expect(model.runEnabled == true)
    }

    @Test("Changing roll or profile does not invoke probe --files before Convert")
    func configurationChangesDoNotProbeBeforeConvert() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let argvPath = directory.appending(path: "argv", directoryHint: .notDirectory)
        let script = """
            printf '%s\\n' "$@" >> '\(argvPath.path)'
            case "$*" in
              *--files*) echo 'files probe should not run yet'; exit 1 ;;
              *) echo '\(Self.started)'
                 echo '\(Self.catalogueABC)'
                 echo '\(Self.finishedSuccess)' ;;
            esac
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.across = 3
        await model.waitForPendingProbes()

        let argv = try String(contentsOf: argvPath, encoding: .utf8)
        #expect(!argv.contains("--files"))
    }

    @Test("An explicit rig profile choice survives a relaunch")
    func rigProfileIDIsPersisted() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess]
        )
        let defaults = Self.isolatedDefaults()

        let first = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        first.rigProfileID = "pid-1"

        let second = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        await second.waitForPendingProbes()

        #expect(second.rigProfileID == "pid-1")
    }

    @Test("validateSelection carries --rig when a profile is chosen")
    func validationProbeCarriesRig() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let argvPath = directory.appending(path: "argv", directoryHint: .notDirectory)
        let script = """
            printf '%s\\n' "$@" >> '\(argvPath.path)'
            echo '\(Self.started)'
            echo '\(Self.threeFileGroupNoOverlap)'
            echo '\(Self.finishedSuccess)'
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)

        let rollDir = directory.appending(path: "roll", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
        let model = ConfigurationModel(
            runner: CLIRunner(executable: executable), defaults: Self.isolatedDefaults()
        )

        model.inputFolder = directory
        await model.waitForPendingProbes()
        model.rollURL = rollDir
        await model.waitForPendingProbes()
        model.selectedFiles = ["a.NEF", "b.NEF", "c.NEF"]
        model.across = 3
        model.rigProfileID = "pid-1"

        let argvBefore = try String(contentsOf: argvPath, encoding: .utf8)
        #expect(!argvBefore.contains("--rig"))

        #expect(await model.validateSelection())

        let argv = try String(contentsOf: argvPath, encoding: .utf8)
            .split(separator: "\n")
            .map(String.init)
        #expect(argv.contains("--rig"))
        let index = try #require(argv.firstIndex(of: "--rig"))
        #expect(argv[index + 1] == "pid-1")
    }

    // MARK: - Last-folder memory

    @Test("The input folder is remembered across model instances")
    func inputFolderIsPersisted() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let executable = try Self.fakeProbeExecutable(
            in: directory,
            catalogueOnly: [Self.started, Self.catalogueABC, Self.finishedSuccess]
        )
        let defaults = Self.isolatedDefaults()

        let first = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        first.inputFolder = directory
        await first.waitForPendingProbes()

        let second = ConfigurationModel(runner: CLIRunner(executable: executable), defaults: defaults)
        await second.waitForPendingProbes()

        #expect(second.inputFolder?.standardizedFileURL == directory.standardizedFileURL)
        #expect(second.catalogue == ["a.NEF", "b.NEF", "c.NEF"])
    }

    // MARK: - Grid dimensions (protocol 10)

    @Test("down defaults to 1 and perNegative is computed from across x down")
    func downDefaultsToOne() {
        let model = ConfigurationModel(
            runner: CLIRunner(executable: Bundle.main.bundleURL), defaults: Self.isolatedDefaults()
        )
        #expect(model.down == 1)
        #expect(model.across == nil)
        #expect(model.perNegative == nil)

        model.across = 3
        #expect(model.perNegative == 3)

        model.down = 2
        #expect(model.perNegative == 6)
    }

    @Test("runEnabled stays off until across is chosen, at any down")
    func runEnabledGatesOnAcross() {
        let model = ConfigurationModel(
            runner: CLIRunner(executable: Bundle.main.bundleURL), defaults: Self.isolatedDefaults()
        )
        #expect(model.runEnabled == false)
        model.down = 2
        #expect(model.runEnabled == false)
        model.across = 3
        #expect(model.perNegative == 6)
    }

    @Test("choosing down clamps across so the product stays within 12")
    func downClampsAcross() {
        let model = ConfigurationModel(
            runner: CLIRunner(executable: Bundle.main.bundleURL), defaults: Self.isolatedDefaults()
        )
        model.across = 12
        #expect(model.perNegative == 12)
        model.down = 2
        #expect(model.across == 6)
        #expect(model.perNegative == 12)
    }
}

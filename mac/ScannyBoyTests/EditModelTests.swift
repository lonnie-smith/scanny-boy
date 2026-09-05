import Foundation
import Testing

@testable import ScannyBoy

/// Drives `EditModel` against a fake `roll info` response, in the same style
/// `ConfigurationModelTests` and `RollLibraryTests` already use for a
/// synthetic helper.
@Suite("Edit model (Chunk P3-12)")
@MainActor
struct EditModelTests {
    private static func isolatedRunner(rollInfoLines: [String]) throws -> CLIRunner {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        func echoLines(_ lines: [String]) -> String {
            lines.map { "echo '\($0)'" }.joined(separator: "\n")
        }
        let executable = try TestSupport.writeTestExecutable(echoLines(rollInfoLines), in: directory)
        return CLIRunner(executable: executable)
    }

    /// One `negative` object for the `roll_info` manifest, with every field
    /// `EditModel` reads.
    private static func negativeJSON(
        negativeID: String,
        sequence: Int?,
        status: String = "completed",
        intended: String?,
        applied: String?,
        rotation: Int = 0,
        flipped: Bool = false,
        previewPath: String? = nil,
        metadata: String? = nil,
        toneGradeR: Double? = nil,
        toneSnapGamma: Double? = nil
    ) -> String {
        let sequenceJSON = sequence.map { "\($0)" } ?? "null"
        let intendedJSON = intended.map { "\"\($0)\"" } ?? "null"
        let appliedJSON = applied.map { "\"\($0)\"" } ?? "null"
        let previewJSON = previewPath.map { "\"\($0)\"" } ?? "null"
        let metadataJSON = metadata ?? "null"
        let toneGradeJSON = toneGradeR.map { "\($0)" } ?? "null"
        let toneSnapJSON = toneSnapGamma.map { "\($0)" } ?? "null"
        return """
            {"negative_id":"\(negativeID)","run_id":"r","sequence":\(sequenceJSON),\
            "members":["a.NEF"],\
            "expected_output":"\(negativeID).tif","status":"\(status)",\
            "output":{"name":"\(negativeID).tif","size":1,"sha256":"\(String(repeating: "a", count: 64))","width":1,"height":1},\
            "frames":[],"pairs":[],"global_rms_px":null,"canvas":null,"valid_rect":null,\
            "fill_color":[0,0,0],"rebate_deviation_px":null,"error_code":null,"error_message":null,\
            "capture_time":{"source_datetime_original":null,\
            "intended_datetime_original":\(intendedJSON),\
            "applied_datetime_original":\(appliedJSON),"date_override":null},\
            "metadata":\(metadataJSON),\
            "preview_path":\(previewJSON),\
            "rotation_quarter_turns":\(rotation),"flipped_horizontally":\(flipped),\
            "tone_grade_r":\(toneGradeJSON),"tone_snap_gamma":\(toneSnapJSON)}
            """
    }

    private static func rollInfoEvent(
        negatives: [String], metadata: String? = nil
    ) -> String {
        let metadataJSON = metadata ?? #"{"roll_capture_date":null,"last_applied_at":null,"city":null,"state":null,"camera":null,"lens":null,"caption":null}"#
        let manifest = """
            {"manifest_format_version":5,"manifest_kind":"roll","scanny_boy_version":"0.3.0",\
            "roll_id":"roll-1","roll_name":"Test Roll",\
            "created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z",\
            "processing_params":{},\
            "icc_profile":{"name":"x.icc","sha256":"\(String(repeating: "b", count: 64))"},\
            "stitch_params":{},"runs":[],"sources":[],\
            "negatives":[\(negatives.joined(separator: ","))],\
            "metadata":\(metadataJSON)}
            """
        return TestEvents.line(#"{"event":"roll_info","manifest":\#(manifest)}"#)
    }

    @Test("Effective metadata resolves the live fallback")
    func testEffectiveMetadataResolvesLiveFallback() async throws {
        // The roll carries the fallback city; the second negative has its
        // own explicit value that must win.
        let rollMetadata = #"{"roll_capture_date":null,"last_applied_at":null,"city":"Porto","state":null,"camera":null,"lens":null,"caption":null}"#
        let inherited = Self.negativeJSON(
            negativeID: "n1", sequence: 1, intended: nil, applied: nil
        )
        let explicit = Self.negativeJSON(
            negativeID: "n2", sequence: 2, intended: nil, applied: nil,
            metadata: #"{"city":"Lisbon","state":null,"camera":null,"lens":null,"caption":null}"#
        )
        let runner = try Self.isolatedRunner(
            rollInfoLines: [Self.rollInfoEvent(negatives: [inherited, explicit], metadata: rollMetadata)]
        )
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        #expect(model.effectiveValue(of: model.visibleNegatives[0], field: "city") == "Porto")
        #expect(model.effectiveValue(of: model.visibleNegatives[1], field: "city") == "Lisbon")
        #expect(model.effectiveValues(of: model.visibleNegatives, field: "city").count == 2)
        #expect(model.effectiveValues(of: [model.visibleNegatives[0]], field: "state").count == 1)
    }

    @Test("Unranked negatives sort after ranked ones, and every negative is visible")
    func testUnrankedNegativesSortAfterRankedOnes() async throws {
        let unranked = Self.negativeJSON(
            negativeID: "n1", sequence: nil, intended: nil, applied: nil
        )
        let ranked = Self.negativeJSON(
            negativeID: "n2", sequence: 1, intended: nil, applied: nil
        )
        let runner = try Self.isolatedRunner(
            rollInfoLines: [Self.rollInfoEvent(negatives: [unranked, ranked])]
        )
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives.map(\.negativeID) == ["n2", "n1"])
    }

    @Test("The rectification block decodes tolerantly: present, absent, and malformed")
    func testRectificationDecodesTolerantly() async throws {
        let withRect = Self.negativeJSON(
            negativeID: "n1", sequence: 1, intended: nil, applied: nil
        ).replacingOccurrences(
            of: "\"rebate_deviation_px\":null",
            with: """
            "rebate_deviation_px":null,\
            "rectification":{"l":[3.1e-07,-3.8e-07],"centre":[3032.0,2020.0],\
            "frame_size":[4040,6064],"rms_before_px":1.41,"rms_after_px":0.83,\
            "relative_improvement":0.41,"pair_count":3}
            """
        )
        let withMalformed = Self.negativeJSON(
            negativeID: "n2", sequence: 2, intended: nil, applied: nil
        ).replacingOccurrences(
            of: "\"rebate_deviation_px\":null",
            with: "\"rebate_deviation_px\":null,\"rectification\":{\"l\":[1.0]}"
        )
        let runner = try Self.isolatedRunner(
            rollInfoLines: [
                Self.rollInfoEvent(negatives: [withRect, withMalformed])
            ]
        )
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        let rectified = model.visibleNegatives.first { $0.negativeID == "n1" }
        #expect(rectified?.rectification?.lX == 3.1e-07)
        #expect(rectified?.rectification?.relativeImprovement == 0.41)
        // A malformed block is no block: nil, never a crash.
        let rejected = model.visibleNegatives.first { $0.negativeID == "n2" }
        #expect(rejected?.rectification == nil)
    }

    @Test("Refresh re-reads the manifest a run just rewrote")
    func testRefreshPicksUpNegativesAddedAfterTheFirstFetch() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let empty = Self.rollInfoEvent(negatives: [])
        let fresh = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(
                negativeID: "n1", sequence: 1,
                intended: "2026-08-02T12:00:00", applied: nil
            ),
            Self.negativeJSON(
                negativeID: "n2", sequence: 2,
                intended: "2026-08-02T12:00:01", applied: nil
            ),
        ])
        // The fake helper's first `roll info` sees the empty roll the
        // user just created; every later one sees the two negatives a
        // run has since stitched into it. The marker lives next to the
        // script itself: the helper's working directory is not writable
        // (or shared), so a bare relative path would never flip.
        let marker = directory.appending(path: "second-call").path
        let script = """
            if [ -f '\(marker)' ]; then
              echo '\(fresh)'
            else
              : > '\(marker)'
              echo '\(empty)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = EditModel(runner: CLIRunner(executable: executable))

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        #expect(model.visibleNegatives.isEmpty)

        // `ContentView`'s run-completion tail calls exactly this; the
        // roll URL itself never changes, so nothing else would refetch.
        model.refresh()
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives.map(\.negativeID) == ["n1", "n2"])
    }

    // MARK: - Delete

    /// A helper whose `edit delete` invocations emit the delete stream and
    /// whose `roll info` invocations flip from the full manifest to the
    /// post-delete one, via a marker beside the script (the helper's own
    /// working directory is not writable).
    private static func deleteRunner(
        _ directory: URL, deleting deletedID: String, negatives: [String]
    ) throws -> CLIRunner {
        func negative(_ id: String, sequence: Int) -> String {
            Self.negativeJSON(negativeID: id, sequence: sequence, intended: nil, applied: nil)
        }
        let initial = Self.rollInfoEvent(
            negatives: negatives.enumerated().map { index, id in
                negative(id, sequence: index + 1)
            }
        )
        let fresh = Self.rollInfoEvent(
            negatives: negatives.filter { $0 != deletedID }.enumerated().map { index, id in
                negative(id, sequence: index + 1)
            }
        )
        let marker = directory.appending(path: "deleted").path
        let script = """
            if [ "$1" = "edit" ]; then
              echo '{"protocol_version":10,"event":"started","command":"edit delete"}'
              echo '{"protocol_version":10,"event":"negative_deleted","negative_id":"\(deletedID)","output":"\(deletedID).tif"}'
              echo '{"protocol_version":10,"event":"finished","status":"success","exit_status":0}'
            else
              if [ -f '\(marker)' ]; then
                echo '\(fresh)'
              else
                : > '\(marker)'
                echo '\(initial)'
              fi
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    @Test("Delete removes the negative and selects its neighbour")
    func testDeleteRemovesTheNegativeAndSelectsItsNeighbour() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.deleteRunner(directory, deleting: "n1", negatives: ["n1", "n2", "n3"])
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        let first = model.selectedNegative

        await model.delete([first!])
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives.map(\.negativeID) == ["n2", "n3"])
        #expect(model.selectedNegative?.negativeID == "n2")
    }

    @Test("Deleting the last negative selects the previous one")
    func testDeletingTheLastNegativeSelectsThePreviousOne() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.deleteRunner(directory, deleting: "n2", negatives: ["n1", "n2"])
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        // Select the last negative explicitly before deleting it.
        model.selectedNegativeID = "n2"

        await model.delete([model.selectedNegative!])
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives.map(\.negativeID) == ["n1"])
        #expect(model.selectedNegative?.negativeID == "n1")
    }

    @Test("A failed delete leaves the roll and the selection alone")
    func testFailedDeleteLeavesTheRollAlone() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        // The helper never emits `negative_deleted`: the CLI refused.
        let alone = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ]; then
              echo '{"protocol_version":10,"event":"started","command":"edit delete"}'
              echo '{"protocol_version":10,"event":"error","code":"ROLL_NOT_FOUND","message":"gone"}'
              echo '{"protocol_version":10,"event":"finished","status":"failed","exit_status":1}'
            else
              echo '\(alone)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = EditModel(runner: CLIRunner(executable: executable))

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        let selected = model.selectedNegative

        await model.delete([selected!])
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives.map(\.negativeID) == ["n1"])
        #expect(model.selectedNegative?.negativeID == "n1")
        #expect(!model.isDeleting)
    }

    // MARK: - Multi-select

    /// Three ranked negatives n1..n3 in every test below.
    private static func multiSelectModel(
        _ runner: CLIRunner
    ) async throws -> EditModel {
        let model = EditModel(runner: runner)
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        return model
    }

    private static func threeNegativeRunner() throws -> CLIRunner {
        try isolatedRunner(rollInfoLines: [
            rollInfoEvent(negatives: [
                negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil),
                negativeJSON(negativeID: "n2", sequence: 2, intended: nil, applied: nil),
                negativeJSON(negativeID: "n3", sequence: 3, intended: nil, applied: nil),
            ])
        ])
    }

    @Test("A plain click selects one frame; shift-click extends a range from the anchor")
    func testPlainAndShiftClickSelection() async throws {
        let model = try await Self.multiSelectModel(try Self.threeNegativeRunner())

        model.select("n2")
        #expect(model.isSelected("n2"))
        #expect(!model.isSelected("n1") && !model.isSelected("n3"))

        // Shift extends from the anchor (n2, last clicked) backwards.
        model.select("n1", extendingRange: true)
        #expect(model.isSelected("n1") && model.isSelected("n2") && !model.isSelected("n3"))

        // The anchor never moved, so another shift re-extends from n2.
        model.select("n3", extendingRange: true)
        #expect(!model.isSelected("n1") && model.isSelected("n2") && model.isSelected("n3"))

        // A fresh anchor lets one shift span everything.
        model.select("n1")
        model.select("n3", extendingRange: true)
        #expect(model.isSelected("n1") && model.isSelected("n2") && model.isSelected("n3"))
    }

    @Test("Command-click toggles frames in and out of the selection")
    func testCommandClickToggles() async throws {
        let model = try await Self.multiSelectModel(try Self.threeNegativeRunner())

        model.select("n1")
        model.select("n3", additive: true)
        #expect(model.isSelected("n1") && model.isSelected("n3") && !model.isSelected("n2"))

        // Toggling n3 again removes it.
        model.select("n3", additive: true)
        #expect(model.isSelected("n1") && !model.isSelected("n2") && !model.isSelected("n3"))
    }

    @Test("Cmd-A selects all, Cmd-D deselects all but keeps the anchor")
    func testSelectAllAndDeselectAll() async throws {
        let model = try await Self.multiSelectModel(try Self.threeNegativeRunner())
        model.select("n2")

        model.selectAll()
        #expect(model.selectionTargets.map(\.negativeID) == ["n1", "n2", "n3"])
        #expect(model.selectedNegative?.negativeID == "n2")

        model.deselectAll()
        #expect(model.selectionTargets.map(\.negativeID) == ["n2"])
    }

    @Test("Arrow navigation collapses a multi-selection to one frame")
    func testKeyboardNavigationCollapsesSelection() async throws {
        let model = try await Self.multiSelectModel(try Self.threeNegativeRunner())
        model.select("n1")
        model.selectAll()

        model.selectNext()

        #expect(model.selectionTargets.map(\.negativeID) == ["n2"])
    }

    @Test("An empty selection acts on the previewed frame alone")
    func testEmptySelectionFallsBackToAnchor() async throws {
        let model = try await Self.multiSelectModel(try Self.threeNegativeRunner())

        #expect(model.selectionTargets.map(\.negativeID) == ["n1"])
    }

    // MARK: - Batch rotate

    /// A helper whose `edit rotate` emits one `edit_recorded` per `--negative`
    /// argument it received (echoing them back so the test can assert the
    /// batch reached the CLI), and whose `roll info` flips to a rotated
    /// manifest after the first edit.
    private static func batchRotateRunner(
        _ directory: URL, negativeIDs: [String]
    ) throws -> CLIRunner {
        let events = negativeIDs.map { id in
            """
            {"protocol_version":10,"event":"edit_recorded","negative_id":"\(id)",\
            "edit":{"id":1,"negative_id":"\(id)","position":1,"op":"rotate",\
            "params":{"direction":"cw"},"created_at":"2026-09-01T00:00:00Z"},\
            "rotation_quarter_turns":1,"flipped_horizontally":false,"preview_path":null}
            """
        }
        let initial = rollInfoEvent(negatives: negativeIDs.enumerated().map { index, id in
            negativeJSON(negativeID: id, sequence: index + 1, intended: nil, applied: nil)
        })
        // After the edit the CLI's own manifest would carry the net turns.
        let rotated = rollInfoEvent(negatives: negativeIDs.enumerated().map { index, id in
            negativeJSON(
                negativeID: id, sequence: index + 1, intended: nil, applied: nil,
                rotation: 1, flipped: false
            )
        })
        let marker = directory.appending(path: "rotated").path
        let script = """
            if [ "$1" = "edit" ]; then
              echo '{"protocol_version":10,"event":"started","command":"edit rotate"}'
              for event in \(events.map { "'\($0)'" }.joined(separator: " ")); do
                echo "$event"
              done
              echo '{"protocol_version":10,"event":"finished","status":"success","exit_status":0}'
            else
              if [ -f '\(marker)' ]; then
                echo '\(rotated)'
              else
                : > '\(marker)'
                echo '\(initial)'
              fi
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    @Test("Batch rotate acts on the whole selection and applies each event")
    func testBatchRotateActsOnTheWholeSelection() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.batchRotateRunner(directory, negativeIDs: ["n1", "n2", "n3"])
        let model = try await Self.multiSelectModel(runner)
        model.select("n1")
        model.select("n3", additive: true)

        await model.rotate(model.selectionTargets, clockwise: true)
        await model.waitForPendingFetch()

        // The refresh reconciled both negatives to one cw turn.
        #expect(model.visibleNegatives.map(\.rotationQuarterTurns) == [1, 1, 1])
        #expect(model.selectionTargets.map(\.negativeID) == ["n1", "n3"])
    }

    // MARK: - Edit tone

    /// A helper whose `edit tone` emits one `edit_recorded` per
    /// `--negative` argument carrying the tone op's params, and whose
    /// `roll info` flips to a manifest with the tone state after the edit.
    private static func editToneRunner(
        _ directory: URL, negativeIDs: [String], gradeR: Double?, snapGamma: Double?
    ) throws -> CLIRunner {
        let gradeJSON = gradeR.map { "\($0)" } ?? "null"
        let snapJSON = snapGamma.map { "\($0)" } ?? "null"
        let events = negativeIDs.map { id in
            """
            {"protocol_version":10,"event":"edit_recorded","negative_id":"\(id)",\
            "edit":{"id":1,"negative_id":"\(id)","position":1,"op":"tone",\
            "params":{"grade_r":\(gradeJSON),"snap_gamma":\(snapJSON)},"created_at":"2026-09-01T00:00:00Z"},\
            "rotation_quarter_turns":0,"flipped_horizontally":false,"preview_path":null}
            """
        }
        let initial = rollInfoEvent(negatives: negativeIDs.enumerated().map { index, id in
            negativeJSON(negativeID: id, sequence: index + 1, intended: nil, applied: nil)
        })
        let toned = rollInfoEvent(negatives: negativeIDs.enumerated().map { index, id in
            negativeJSON(
                negativeID: id, sequence: index + 1, intended: nil, applied: nil,
                toneGradeR: gradeR, toneSnapGamma: snapGamma
            )
        })
        let marker = directory.appending(path: "toned").path
        let script = """
            if [ "$1" = "edit" ]; then
              echo '{"protocol_version":10,"event":"started","command":"edit tone"}'
              for event in \(events.map { "'\($0)'" }.joined(separator: " ")); do
                echo "$event"
              done
              echo '{"protocol_version":10,"event":"finished","status":"success","exit_status":0}'
            else
              if [ -f '\(marker)' ]; then
                echo '\(toned)'
              else
                : > '\(marker)'
                echo '\(initial)'
              fi
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    @Test("setTone records the adjustment on the whole selection")
    func testSetToneAppliesToTheWholeSelection() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.editToneRunner(directory, negativeIDs: ["n1", "n2"], gradeR: 90, snapGamma: 0.2)
        let model = try await Self.multiSelectModel(runner)

        await model.setTone(model.selectionTargets, gradeR: 90, snapGamma: 0.2)
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives.map(\.toneGradeR) == [90.0, 90.0])
        #expect(model.visibleNegatives.map(\.toneSnapGamma) == [0.2, 0.2])
    }

    @Test("setTone with nils resets to the flat look")
    func testSetToneReset() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.editToneRunner(directory, negativeIDs: ["n1"], gradeR: nil, snapGamma: nil)
        let model = try await Self.multiSelectModel(runner)

        await model.setTone(model.selectionTargets, gradeR: nil, snapGamma: nil)
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives[0].toneGradeR == nil)
        #expect(model.visibleNegatives[0].toneSnapGamma == nil)
        #expect(EditModel.renderGeneration(of: model.visibleNegatives[0]).hasSuffix("#flat"))
    }

    @Test("A rotate event leaves the tone state alone")
    func testRotateLeavesToneAlone() async throws {
        // A manifest with tone recorded; the rotate event carries no tone
        // keys, so the in-place update must keep them.
        let initial = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(
                negativeID: "n1", sequence: 1, intended: nil, applied: nil,
                toneGradeR: 90, toneSnapGamma: 0.2
            ),
        ])
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let event =
            """
            {"protocol_version":10,"event":"edit_recorded","negative_id":"n1",\
            "edit":{"id":2,"negative_id":"n1","position":2,"op":"rotate",\
            "params":{"direction":"cw"},"created_at":"2026-09-01T00:00:01Z"},\
            "rotation_quarter_turns":1,"flipped_horizontally":false,"preview_path":null}
            """
        let rotated = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(
                negativeID: "n1", sequence: 1, intended: nil, applied: nil,
                rotation: 1, toneGradeR: 90, toneSnapGamma: 0.2
            ),
        ])
        let marker = directory.appending(path: "rotated").path
        let script = """
            if [ "$1" = "edit" ]; then
              echo '{"protocol_version":10,"event":"started","command":"edit rotate"}'
              echo '\(event)'
              echo '{"protocol_version":10,"event":"finished","status":"success","exit_status":0}'
            else
              if [ -f '\(marker)' ]; then
                echo '\(rotated)'
              else
                : > '\(marker)'
                echo '\(initial)'
              fi
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = try await Self.multiSelectModel(CLIRunner(executable: executable))

        await model.rotate(model.selectionTargets, clockwise: true)
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives[0].rotationQuarterTurns == 1)
        #expect(model.visibleNegatives[0].toneGradeR == 90)
        #expect(model.visibleNegatives[0].toneSnapGamma == 0.2)
    }

    @Test("The render generation token carries the tone state")
    func testRenderGenerationCarriesTone() {
        let flat = Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
        let toned = Self.multiNegative(id: "n1", toneGradeR: 90, toneSnapGamma: 0.2)
        let other = Self.multiNegative(id: "n1", toneGradeR: 70, toneSnapGamma: 0.2)

        #expect(EditModel.renderGeneration(of: flat) != EditModel.renderGeneration(of: toned))
        #expect(EditModel.renderGeneration(of: toned) != EditModel.renderGeneration(of: other))
        #expect(EditModel.renderGeneration(of: flat).hasSuffix("#flat"))
    }

    private static func multiNegative(
        id: String, toneGradeR: Double?, toneSnapGamma: Double?
    ) -> RollManifest.Negative {
        RollManifest.Negative(
            negativeID: id,
            runID: "r",
            sequence: 1,
            members: ["a.NEF"],
            expectedOutput: "\(id).tif",
            status: "completed",
            output: RollManifest.Output(
                name: "\(id).tif", size: 1, sha256: String(repeating: "a", count: 64),
                width: 1, height: 1
            ),
            captureTime: RollManifest.CaptureTime(
                sourceDatetimeOriginal: nil, intendedDatetimeOriginal: nil,
                appliedDatetimeOriginal: nil, dateOverride: nil
            ),
            metadata: .empty,
            globalRMSPixels: nil,
            rebateDeviationPixels: nil,
            previewPath: nil,
            rotationQuarterTurns: 0,
            flippedHorizontally: false,
            rectification: nil,
            toneGradeR: toneGradeR,
            toneSnapGamma: toneSnapGamma
        )
    }
}

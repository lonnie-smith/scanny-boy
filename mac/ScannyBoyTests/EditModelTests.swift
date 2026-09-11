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
        toneSnapGamma: Double? = nil,
        toneDensity: Double? = nil,
        toneToe: Double? = nil
    ) -> String {
        let sequenceJSON = sequence.map { "\($0)" } ?? "null"
        let intendedJSON = intended.map { "\"\($0)\"" } ?? "null"
        let appliedJSON = applied.map { "\"\($0)\"" } ?? "null"
        let previewJSON = previewPath.map { "\"\($0)\"" } ?? "null"
        let metadataJSON = metadata ?? "null"
        let toneGradeJSON = toneGradeR.map { "\($0)" } ?? "null"
        let toneSnapJSON = toneSnapGamma.map { "\($0)" } ?? "null"
        let toneDensityJSON = toneDensity.map { "\($0)" } ?? "null"
        let toneToeJSON = toneToe.map { "\($0)" } ?? "null"
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
            "tone_grade_r":\(toneGradeJSON),"tone_snap_gamma":\(toneSnapJSON),\
            "tone_density":\(toneDensityJSON),"tone_shadow_density":null,\
            "tone_highlight_density":null,"tone_toe":\(toneToeJSON),\
            "tone_toe_width":null,"tone_shoulder":null,"tone_shoulder_width":null}
            """
    }

    private static func toneParamsJSON(from adjustment: ToneAdjustment?) -> String {
        guard let adjustment else {
            return """
            "grade_r":null,"snap_gamma":null,"density":null,"shadow_density":null,\
            "highlight_density":null,"toe":null,"toe_width":null,"shoulder":null,\
            "shoulder_width":null
            """
        }
        return """
            "grade_r":\(adjustment.gradeR),"snap_gamma":\(adjustment.snapGamma),\
            "density":\(adjustment.density),"shadow_density":\(adjustment.shadowDensity),\
            "highlight_density":\(adjustment.highlightDensity),"toe":\(adjustment.toe),\
            "toe_width":\(adjustment.toeWidth),"shoulder":\(adjustment.shoulder),\
            "shoulder_width":\(adjustment.shoulderWidth)
            """
    }

    private static func rollInfoEvent(
        negatives: [String], metadata: String? = nil
    ) -> String {
        let metadataJSON = metadata ?? #"{"roll_capture_date":null,"last_applied_at":null,"film":null,"iso":null,"city":null,"state":null,"camera":null,"lens":null,"caption":null}"#
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
        let rollMetadata = #"{"roll_capture_date":null,"last_applied_at":null,"film":null,"iso":null,"city":"Porto","state":null,"camera":null,"lens":null,"caption":null}"#
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
            if [ "$1" = "edit" ] && [ "$2" = "delete" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit delete"}'
              echo '{"protocol_version":21,"event":"negative_deleted","negative_id":"\(deletedID)","output":"\(deletedID).tif"}'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
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
              echo '{"protocol_version":21,"event":"started","command":"edit delete"}'
              echo '{"protocol_version":21,"event":"error","code":"ROLL_NOT_FOUND","message":"gone"}'
              echo '{"protocol_version":21,"event":"finished","status":"failed","exit_status":1}'
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
            {"protocol_version":21,"event":"edit_recorded","negative_id":"\(id)",\
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
              echo '{"protocol_version":21,"event":"started","command":"edit rotate"}'
              for event in \(events.map { "'\($0)'" }.joined(separator: " ")); do
                echo "$event"
              done
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
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
        _ directory: URL, negativeIDs: [String], adjustment: ToneAdjustment?
    ) throws -> CLIRunner {
        let paramsJSON = toneParamsJSON(from: adjustment)
        let events = negativeIDs.map { id in
            """
            {"protocol_version":21,"event":"edit_recorded","negative_id":"\(id)",\
            "edit":{"id":1,"negative_id":"\(id)","position":1,"op":"tone",\
            "params":{\(paramsJSON)},"created_at":"2026-09-01T00:00:00Z"},\
            "rotation_quarter_turns":0,"flipped_horizontally":false,"preview_path":null}
            """
        }
        let initial = rollInfoEvent(negatives: negativeIDs.enumerated().map { index, id in
            negativeJSON(negativeID: id, sequence: index + 1, intended: nil, applied: nil)
        })
        let toned = rollInfoEvent(negatives: negativeIDs.enumerated().map { index, id in
            negativeJSON(
                negativeID: id, sequence: index + 1, intended: nil, applied: nil,
                toneGradeR: adjustment?.gradeR, toneSnapGamma: adjustment?.snapGamma,
                toneDensity: adjustment?.density, toneToe: adjustment?.toe
            )
        })
        let marker = directory.appending(path: "toned").path
        let script = """
            if [ "$1" = "edit" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit tone"}'
              for event in \(events.map { "'\($0)'" }.joined(separator: " ")); do
                echo "$event"
              done
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
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
        let runner = try Self.editToneRunner(
            directory, negativeIDs: ["n1", "n2"],
            adjustment: ToneAdjustment(gradeR: 90, snapGamma: 0.2, density: 1,
                                       shadowDensity: 0, highlightDensity: 0,
                                       toe: 0, toeWidth: 2.5, shoulder: 0, shoulderWidth: 2.5)
        )
        let model = try await Self.multiSelectModel(runner)

        await model.setTone(
            model.selectionTargets,
            adjustment: ToneAdjustment(gradeR: 90, snapGamma: 0.2, density: 1,
                                     shadowDensity: 0, highlightDensity: 0,
                                     toe: 0, toeWidth: 2.5, shoulder: 0, shoulderWidth: 2.5)
        )
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
        let runner = try Self.editToneRunner(directory, negativeIDs: ["n1"], adjustment: nil)
        let model = try await Self.multiSelectModel(runner)

        await model.setTone(model.selectionTargets, adjustment: nil)
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives[0].toneGradeR == nil)
        #expect(model.visibleNegatives[0].toneSnapGamma == nil)
        #expect(EditModel.renderGeneration(of: model.visibleNegatives[0]).hasSuffix("#flat#neutral#none#none#none"))
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
            {"protocol_version":21,"event":"edit_recorded","negative_id":"n1",\
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
            if [ "$1" = "edit" ] && [ "$2" = "rotate" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit rotate"}'
              echo '\(event)'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
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
        let other = Self.multiNegative(id: "n1", toneGradeR: 70, toneSnapGamma: 0.2, toneToe: 0.5)

        #expect(EditModel.renderGeneration(of: flat) != EditModel.renderGeneration(of: toned))
        #expect(EditModel.renderGeneration(of: toned) != EditModel.renderGeneration(of: other))
        #expect(EditModel.renderGeneration(of: flat).hasSuffix("#flat#neutral#none#none#none"))
    }

    @Test("The render generation token carries the colour state")
    func testRenderGenerationCarriesColor() {
        let neutral = Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
        let separated = Self.multiNegative(
            id: "n1", toneGradeR: nil, toneSnapGamma: nil, colorDyeSeparation: 1.2
        )

        #expect(
            EditModel.renderGeneration(of: neutral)
                != EditModel.renderGeneration(of: separated)
        )
    }

    @Test("The render generation token carries the highlight cast removal strength")
    func testRenderGenerationCarriesCastRemovalHighlights() {
        let shadowOnly = Self.multiNegative(
            id: "n1",
            toneGradeR: nil,
            toneSnapGamma: nil,
            colorDyeSeparation: 1.0,
            colorCastRemovalHighlights: 0
        )
        let withHighlights = Self.multiNegative(
            id: "n1",
            toneGradeR: nil,
            toneSnapGamma: nil,
            colorDyeSeparation: 1.0,
            colorCastRemovalHighlights: 0.5
        )

        #expect(
            EditModel.renderGeneration(of: shadowOnly)
                != EditModel.renderGeneration(of: withHighlights)
        )
    }

    @Test("A roll info payload without highlight cast removal defaults to zero")
    func testColorAdjustmentDefaultsMissingHighlightCastRemoval() throws {
        let manifestJSON = """
        {"roll_id":"roll-1","roll_name":"Roll","created_at":"2026-01-01T00:00:00Z",\
        "updated_at":"2026-01-01T00:00:00Z","runs":[],"metadata":{},\
        "negatives":[{"negative_id":"n1","run_id":"r","members":["a.NEF"],\
        "expected_output":"n1.tif","status":"completed","capture_time":{},\
        "color_wb_cyan":0.1,"color_wb_magenta":0.2,"color_wb_yellow":0,\
        "color_cast_removal":0.3}]}
        """
        let fields = try #require(
            try JSONDecoder().decode(JSONValue.self, from: Data(manifestJSON.utf8)).objectValue
        )
        let manifest = try #require(RollManifest(fields: fields))
        let negative = try #require(manifest.negatives.first)
        let adjustment = try #require(negative.colorAdjustment)
        #expect(adjustment.castRemovalHighlights == 0)
        #expect(adjustment.castRemoval == 0.3)
    }

    @Test("The negative view's cache generation carries the transform but not the tone")
    func testNegativeViewGenerationIgnoresTone() {
        let flat = Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
        let toned = Self.multiNegative(id: "n1", toneGradeR: 90, toneSnapGamma: 0.2)

        #expect(EditModel.negativeViewGeneration(of: flat) == "0#false#none#none")
        #expect(EditModel.negativeViewGeneration(of: flat) == EditModel.negativeViewGeneration(of: toned))
    }

    @Test("renderPreview renders through the CLI and passes --mode negative")
    func testRenderPreviewNegativeMode() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let argsFile = directory.appending(path: "args")
        let rollInfo = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "render-preview" ]; then
              printf '%s\\n' "$@" >> '\(argsFile.path)'
              out=""
              prev=""
              for a in "$@"; do
                if [ "$prev" = "--output" ]; then out="$a"; fi
                prev="$a"
              done
              echo '{"protocol_version":21,"event":"started","command":"edit render-preview"}'
              echo '{"protocol_version":21,"event":"preview_rendered","negative_id":"n1","path":"x","width":2,"height":2}'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
              mkdir -p "$(dirname "$out")"
              printf 'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg==' | base64 -D > "$out"
            else
              echo '\(rollInfo)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = try await Self.multiSelectModel(CLIRunner(executable: executable))
        guard let negative = model.visibleNegatives.first else {
            Issue.record("no negatives in the fake roll")
            return
        }

        let thumbnail = await model.renderPreview(negative, mode: .negative)

        #expect(thumbnail != nil)
        let args = try String(contentsOf: argsFile, encoding: .utf8)
        #expect(args.contains("render-preview"))
        #expect(args.contains("--mode"))
        #expect(args.contains("negative"))
    }

    @Test("renderRegion reads a cached PNG without calling the CLI")
    func testRenderRegionReadsCache() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let argsFile = directory.appending(path: "args")
        let rollInfo = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "render-region" ]; then
              printf '%s\\n' "$@" >> '\(argsFile.path)'
            fi
            echo '\(rollInfo)'
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let cache = PreviewCache(cachesDirectory: directory)
        let model = EditModel(runner: CLIRunner(executable: executable), previewCache: cache)
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        guard let negative = model.visibleNegatives.first else {
            Issue.record("no negatives in the fake roll")
            return
        }

        let rect = CGRect(x: 0, y: 0, width: 4, height: 4)
        let cachedURL = cache.regionURL(
            rollID: "roll-1",
            negativeID: negative.negativeID,
            generation: EditModel.renderGeneration(
                of: negative, cameraColor: model.roll?.cameraColor
            ),
            mode: .positive,
            rect: rect
        )
        try FileManager.default.createDirectory(
            at: cachedURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        let png = Data(
            base64Encoded:
                "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg=="
        )!
        try png.write(to: PreviewCache.legacyRegionPNGURL(from: cachedURL))

        let thumbnail = await model.renderRegion(negative, rect: rect, mode: .positive)

        #expect(thumbnail != nil)
        #expect(!FileManager.default.fileExists(atPath: argsFile.path))
    }

    @Test("renderRegion hits the in-memory cache on a second request")
    func testRenderRegionMemoryCache() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let argsFile = directory.appending(path: "args")
        let rollInfo = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "render-region" ]; then
              printf '%s\\n' "$@" >> '\(argsFile.path)'
            fi
            echo '\(rollInfo)'
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let cache = PreviewCache(cachesDirectory: directory)
        let memory = RegionMemoryCache(maxEntries: 4)
        let model = EditModel(
            runner: CLIRunner(executable: executable),
            previewCache: cache,
            regionMemoryCache: memory
        )
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        guard let negative = model.visibleNegatives.first else {
            Issue.record("no negatives in the fake roll")
            return
        }

        let rect = CGRect(x: 0, y: 0, width: 4, height: 4)
        let cachedURL = cache.regionURL(
            rollID: "roll-1",
            negativeID: negative.negativeID,
            generation: EditModel.renderGeneration(
                of: negative, cameraColor: model.roll?.cameraColor
            ),
            mode: .positive,
            rect: rect
        )
        try FileManager.default.createDirectory(
            at: cachedURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        let png = Data(
            base64Encoded:
                "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg=="
        )!
        try png.write(to: PreviewCache.legacyRegionPNGURL(from: cachedURL))

        let first = await model.renderRegion(negative, rect: rect, mode: .positive)
        let second = await model.renderRegion(negative, rect: rect, mode: .positive)

        #expect(first != nil)
        #expect(second != nil)
        #expect(!FileManager.default.fileExists(atPath: argsFile.path))
    }

    @Test("renderRegion passes the display mode through to the CLI")
    func testRenderRegionCarriesMode() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let argsFile = directory.appending(path: "args")
        let rollInfo = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "render-region" ]; then
              printf '%s\\n' "$@" >> '\(argsFile.path)'
              out=""
              prev=""
              for a in "$@"; do
                if [ "$prev" = "--output" ]; then out="$a"; fi
                prev="$a"
              done
              echo '{"protocol_version":21,"event":"started","command":"edit render-region"}'
              echo '{"protocol_version":21,"event":"region_rendered","negative_id":"n1","path":"x","x":0,"y":0,"width":4,"height":4}'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
              mkdir -p "$(dirname "$out")"
              legacy="${out%.rgba}.png"
              printf 'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP4z8AARAwQCgAf7gP9i18U1AAAAABJRU5ErkJggg==' | base64 -D > "$legacy"
            else
              echo '\(rollInfo)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let cache = PreviewCache(cachesDirectory: directory)
        let model = EditModel(
            runner: CLIRunner(executable: executable),
            previewCache: cache
        )
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        guard let negative = model.visibleNegatives.first else {
            Issue.record("no negatives in the fake roll")
            return
        }

        let thumbnail = await model.renderRegion(
            negative, rect: CGRect(x: 0, y: 0, width: 4, height: 4), mode: .negative
        )

        #expect(thumbnail != nil)
        let args = try String(contentsOf: argsFile, encoding: .utf8)
        #expect(args.contains("render-region"))
        #expect(args.contains("--mode"))
        #expect(args.contains("negative"))
    }

    @Test("scheduleTone debounces rapid slider commits into one CLI round trip")
    func testScheduleToneDebounces() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let counter = directory.appending(path: "tone-count")
        let runner = try Self.countingEditToneRunner(directory, counter: counter)
        let model = try await Self.multiSelectModel(runner)

        model.scheduleTone(
            model.selectionTargets,
            adjustment: ToneAdjustment(gradeR: 90, snapGamma: 0, density: 1,
                                       shadowDensity: 0, highlightDensity: 0,
                                       toe: 0, toeWidth: 2.5, shoulder: 0, shoulderWidth: 2.5)
        )
        model.scheduleTone(
            model.selectionTargets,
            adjustment: ToneAdjustment(gradeR: 100, snapGamma: 0, density: 1,
                                       shadowDensity: 0, highlightDensity: 0,
                                       toe: 0, toeWidth: 2.5, shoulder: 0, shoulderWidth: 2.5)
        )
        try await Task.sleep(for: .milliseconds(250))
        await model.waitForPendingTone()

        let count = Int(try String(contentsOf: counter, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)) ?? 0
        #expect(count == 1)
        #expect(model.visibleNegatives[0].toneGradeR == 100)
    }

    @Test("commitTone supersedes an in-flight tone session")
    func testCommitToneSupersedesInFlightSession() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let counter = directory.appending(path: "tone-count")
        let runner = try Self.slowCountingEditToneRunner(directory, counter: counter)
        let model = try await Self.multiSelectModel(runner)

        let first = Task {
            await model.commitTone(
                model.selectionTargets,
                adjustment: ToneAdjustment(gradeR: 90, snapGamma: 0, density: 1,
                                           shadowDensity: 0, highlightDensity: 0,
                                           toe: 0, toeWidth: 2.5, shoulder: 0, shoulderWidth: 2.5)
            )
        }
        try await Task.sleep(for: .milliseconds(20))
        await model.commitTone(
            model.selectionTargets,
            adjustment: ToneAdjustment(gradeR: 100, snapGamma: 0, density: 1,
                                       shadowDensity: 0, highlightDensity: 0,
                                       toe: 0, toeWidth: 2.5, shoulder: 0, shoulderWidth: 2.5)
        )
        await first.value
        await model.waitForPendingTone()

        #expect(model.visibleNegatives[0].toneGradeR == 100)
    }

    /// Parses `--grade` / `--snap` from the fake CLI, increments `counter`
    /// on each `edit tone`, and emits matching `edit_recorded` events.
    private static func countingEditToneRunner(
        _ directory: URL, counter: URL
    ) throws -> CLIRunner {
        let initial = rollInfoEvent(negatives: [
            negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil),
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "tone" ]; then
              grade=""
              snap=""
              while [ $# -gt 0 ]; do
                case "$1" in
                  --grade) grade="$2"; shift 2 ;;
                  --snap) snap="$2"; shift 2 ;;
                  *) shift ;;
                esac
              done
              count=$(cat '\(counter.path)' 2>/dev/null || echo 0)
              echo $((count + 1)) > '\(counter.path)'
              echo '{"protocol_version":21,"event":"started","command":"edit tone"}'
              echo '{"protocol_version":21,"event":"edit_recorded","negative_id":"n1",\
            "edit":{"id":1,"negative_id":"n1","position":1,"op":"tone",\
            "params":{"grade_r":'"$grade"',"snap_gamma":'"$snap"',"density":1,"shadow_density":0,\
            "highlight_density":0,"toe":0,"toe_width":2.5,"shoulder":0,"shoulder_width":2.5},\
            "created_at":"2026-09-01T00:00:00Z"},\
            "rotation_quarter_turns":0,"flipped_horizontally":false,"preview_path":null}'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            else
              echo '\(initial)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    /// Like `countingEditToneRunner`, but sleeps before finishing so a
    /// superseding commit can cancel the first session.
    private static func slowCountingEditToneRunner(
        _ directory: URL, counter: URL
    ) throws -> CLIRunner {
        let initial = rollInfoEvent(negatives: [
            negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil),
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "tone" ]; then
              grade=""
              snap=""
              while [ $# -gt 0 ]; do
                case "$1" in
                  --grade) grade="$2"; shift 2 ;;
                  --snap) snap="$2"; shift 2 ;;
                  *) shift ;;
                esac
              done
              count=$(cat '\(counter.path)' 2>/dev/null || echo 0)
              echo $((count + 1)) > '\(counter.path)'
              echo '{"protocol_version":21,"event":"started","command":"edit tone"}'
              sleep 0.2
              echo '{"protocol_version":21,"event":"edit_recorded","negative_id":"n1",\
            "edit":{"id":1,"negative_id":"n1","position":1,"op":"tone",\
            "params":{"grade_r":'"$grade"',"snap_gamma":'"$snap"',"density":1,"shadow_density":0,\
            "highlight_density":0,"toe":0,"toe_width":2.5,"shoulder":0,"shoulder_width":2.5},\
            "created_at":"2026-09-01T00:00:00Z"},\
            "rotation_quarter_turns":0,"flipped_horizontally":false,"preview_path":null}'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            else
              echo '\(initial)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    private static func multiNegative(
        id: String,
        toneGradeR: Double?,
        toneSnapGamma: Double?,
        toneToe: Double? = nil,
        colorDyeSeparation: Double? = nil,
        colorCastRemovalHighlights: Double? = nil
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
            toneSnapGamma: toneSnapGamma,
            toneDensity: toneGradeR == nil ? nil : 1,
            toneShadowDensity: toneGradeR == nil ? nil : 0,
            toneHighlightDensity: toneGradeR == nil ? nil : 0,
            toneToe: toneToe,
            toneToeWidth: toneGradeR == nil ? nil : 2.5,
            toneShoulder: toneGradeR == nil ? nil : 0,
            toneShoulderWidth: toneGradeR == nil ? nil : 2.5,
            colorWbCyan: colorDyeSeparation == nil ? nil : 0,
            colorWbMagenta: colorDyeSeparation == nil ? nil : 0,
            colorWbYellow: colorDyeSeparation == nil ? nil : 0,
            colorShadowCyan: nil,
            colorShadowMagenta: nil,
            colorShadowYellow: nil,
            colorHighlightCyan: nil,
            colorHighlightMagenta: nil,
            colorHighlightYellow: nil,
            colorCastRemoval: colorDyeSeparation == nil ? nil : 0,
            colorCastRemovalHighlights: colorCastRemovalHighlights
                ?? (colorDyeSeparation == nil ? nil : 0),
            colorDyeSeparation: colorDyeSeparation,
            colorSeparationDamping: colorDyeSeparation == nil ? nil : 0,
            colorTemperature: nil,
            errorCode: nil,
            errorMessage: nil,
            maxOverlapMAD: nil,
            normalization: nil,
            usedClaheFallback: false,
            gridPitchRatio: nil,
            gridAlignmentRatio: nil
        )
    }
    // MARK: - Spots (protocol 13)

    /// A helper that emits one `spots_reported` event per `edit spots`
    /// invocation (echoing the flags back), and flips the roll's manifest
    /// to carry the matching summary after the first edit.
    private static func spotsRunner(
        _ directory: URL, initialSpots: String
    ) throws -> CLIRunner {
        let initial = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let reviewed = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
                .replacingOccurrences(of: "\"tone_shoulder_width\":null}", with: """
                    "tone_shoulder_width":null,"spots":\(initialSpots)}
                    """)
        ])
        let marker = directory.appending(path: "reviewed").path
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "list-spots" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit list-spots"}'
              echo '\(Self.spotsEvent(spots: initialSpots, repair: false, preview: nil))'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            elif [ "$1" = "edit" ] && [ "$2" = "spots" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit spots"}'
              : > '\(marker)'
              echo '\(Self.spotsEvent(spots: initialSpots, repair: false, preview: "/tmp/preview.png"))'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            else
              if [ -f '\(marker)' ]; then
                echo '\(reviewed)'
              else
                echo '\(initial)'
              fi
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    private static func spotsEvent(spots: String, repair: Bool, preview: String?) -> String {
        let previewJSON = preview.map { "\"\($0)\"" } ?? "null"
        return """
            {"protocol_version":21,"event":"spots_reported","negative_id":"n1",\
            "detector_version":1,"sensitivity":0.5,"repair":\(repair),\
            "spots":\(spots),"found":2,"preview_path":\(previewJSON)}
            """
    }

    private static let twoSpotsJSON = """
        [{"id":1,"kind":"blob","polarity":"dense","rect":[10,12,5,4],"score":9.4,"rejected":false},\
        {"id":2,"kind":"streak","polarity":"thin","rect":[40,30,20,3],"score":7.1,"rejected":false}]
        """

    @Test("A spots_reported event lands in spots and the summary")
    func testSpotsReportedLandsInSpots() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.spotsRunner(directory, initialSpots: Self.twoSpotsJSON)
        let model = EditModel(runner: runner)
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        // The roll fetch itself loaded the displayed negative's set.
        let loaded = try #require(model.spots)
        #expect(loaded.spots.count == 2)
        #expect(loaded.accepted.count == 2)
        #expect(loaded.found == 2)
        #expect(model.visibleNegatives[0].spotsSummary?.count == 2)
    }

    @Test("Rejecting updates the local state without a refetch, then reconciles")
    func testRejectingUpdatesLocalState() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        // The review event echoes back one rejected spot.
        let rejectedJSON = """
            [{"id":1,"kind":"blob","polarity":"dense","rect":[10,12,5,4],"score":9.4,"rejected":true},\
            {"id":2,"kind":"streak","polarity":"thin","rect":[40,30,20,3],"score":7.1,"rejected":false}]
            """
        let initial = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "spots" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit spots"}'
              echo '\(Self.spotsEvent(spots: rejectedJSON, repair: false, preview: nil))'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            else
              echo '\(initial)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = EditModel(runner: CLIRunner(executable: executable))
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        await model.rejectSpot(model.visibleNegatives[0], id: 1)

        // The in-memory state moved without waiting for the refetch.
        let spots = try #require(model.spots)
        #expect(spots.spots[0].rejected == true)
        #expect(spots.accepted.map(\.id) == [2])
        #expect(model.visibleNegatives[0].spotsSummary?.rejected == 1)
    }

    @Test("The render generation changes when repair flips or the rejected count does")
    func testRenderGenerationCarriesSpots() {
        func negative(_ summary: NegativeSpots.Summary?) -> RollManifest.Negative {
            Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
                .with(spotsSummary: summary)
        }
        let none = negative(nil)
        let markersOnly = negative(
            NegativeSpots.Summary(
                detectorVersion: 1, sensitivity: 0.5, repair: false,
                stale: false, count: 2, rejected: 0
            )
        )
        let repaired = negative(
            NegativeSpots.Summary(
                detectorVersion: 1, sensitivity: 0.5, repair: true,
                stale: false, count: 2, rejected: 0
            )
        )
        let partlyRejected = negative(
            NegativeSpots.Summary(
                detectorVersion: 1, sensitivity: 0.5, repair: true,
                stale: false, count: 2, rejected: 1
            )
        )

        #expect(EditModel.renderGeneration(of: none) != EditModel.renderGeneration(of: markersOnly))
        #expect(EditModel.renderGeneration(of: markersOnly) != EditModel.renderGeneration(of: repaired))
        #expect(EditModel.renderGeneration(of: repaired) != EditModel.renderGeneration(of: partlyRejected))
        // The repair applies in both display modes: the negative view's
        // generation moves with it too.
        #expect(EditModel.negativeViewGeneration(of: markersOnly) != EditModel.negativeViewGeneration(of: repaired))
    }

    @Test("setRepair flips the repair switch and refreshes the roll")
    func testSetRepair() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let initial = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "spots" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit spots"}'
              echo '\(Self.spotsEvent(spots: Self.twoSpotsJSON, repair: true, preview: nil))'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            else
              echo '\(initial)'
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        let model = EditModel(runner: CLIRunner(executable: executable))
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        await model.setRepair(model.visibleNegatives[0], on: true)

        #expect(model.spots?.repair == true)
        #expect(model.visibleNegatives[0].spotsSummary?.repair == true)
    }

    @Test("A stale summary keeps the negative-view generation moving")
    func testStaleSummaryAffectsGeneration() {
        let fresh = Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
        let stale = Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
            .with(
                spotsSummary: NegativeSpots.Summary(
                    detectorVersion: 1, sensitivity: 0.5, repair: true,
                    stale: true, count: 0, rejected: 0
                )
            )
        #expect(EditModel.negativeViewGeneration(of: fresh) != EditModel.negativeViewGeneration(of: stale))
    }

    // MARK: - Crop (protocol 19)

    /// A helper whose `edit crop` invocations emit one `edit_recorded`
    /// carrying the net crop report (echoing the flags back), and whose
    /// `roll info` carries the matching report after the first edit.
    private static func cropRunner(
        _ directory: URL, cropReport: String
    ) throws -> CLIRunner {
        let initial = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
        ])
        let cropped = Self.rollInfoEvent(negatives: [
            Self.negativeJSON(negativeID: "n1", sequence: 1, intended: nil, applied: nil)
                .replacingOccurrences(
                    of: "\"tone_shoulder_width\":null}",
                    with: "\"tone_shoulder_width\":null,\"crop\":\(cropReport)}"
                )
        ])
        let marker = directory.appending(path: "cropped").path
        let script = """
            if [ "$1" = "edit" ] && [ "$2" = "crop" ]; then
              echo '{"protocol_version":21,"event":"started","command":"edit crop"}'
              : > '\(marker)'
              echo '{"protocol_version":21,"event":"edit_recorded","negative_id":"n1",\
            "edit":{"id":1,"negative_id":"n1","position":1,"op":"crop","params":{"x":10},\
            "created_at":"2026-09-01T00:00:00Z"},\
            "rotation_quarter_turns":0,"flipped_horizontally":false,\
            "crop":\(cropReport),"preview_path":"/tmp/preview.png"}'
              echo '{"protocol_version":21,"event":"finished","status":"success","exit_status":0}'
            else
              if [ -f '\(marker)' ]; then
                echo '\(cropped)'
              else
                echo '\(initial)'
              fi
            fi
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return CLIRunner(executable: executable)
    }

    private static let cropReportJSON = """
        {"width":50,"height":24,"tilt_deg":2.5,"preset":"35mm"}
        """

    @Test("Apply records the crop and updates the local state")
    func testApplyCropUpdatesTheLocalState() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.cropRunner(directory, cropReport: Self.cropReportJSON)
        let model = EditModel(runner: runner)
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        let anchor = try #require(model.selectedNegative)
        #expect(anchor.crop == nil)

        await model.applyCrop(
            anchor, rect: CGRect(x: 10, y: 8, width: 50, height: 24),
            tiltDegrees: 2.5, preset: "35mm"
        )
        await model.waitForPendingFetch()

        let crop = try #require(model.visibleNegatives[0].crop)
        #expect(crop.width == 50)
        #expect(crop.height == 24)
        #expect(crop.tiltDegrees == 2.5)
        #expect(crop.preset == "35mm")
        #expect(model.isCropping == false)
    }

    @Test("Reset clears the crop")
    func testResetCropClearsTheState() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let runner = try Self.cropRunner(directory, cropReport: "null")
        let model = EditModel(runner: runner)
        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()
        let anchor = try #require(model.selectedNegative)

        await model.resetCrop(anchor)
        await model.waitForPendingFetch()

        #expect(model.visibleNegatives[0].crop == nil)
    }

    @Test("The render generation changes when the crop does")
    func testRenderGenerationCarriesCrop() {
        func negative(_ crop: CropState?) -> RollManifest.Negative {
            var base = Self.multiNegative(id: "n1", toneGradeR: nil, toneSnapGamma: nil)
            base.crop = crop
            return base
        }
        let none = negative(nil)
        let cropped = negative(CropState(width: 50, height: 24, tiltDegrees: 0, preset: nil))
        let tilted = negative(CropState(width: 50, height: 24, tiltDegrees: 3.5, preset: nil))

        #expect(EditModel.renderGeneration(of: none) != EditModel.renderGeneration(of: cropped))
        #expect(EditModel.renderGeneration(of: cropped) != EditModel.renderGeneration(of: tilted))
        // The crop changes which pixels the display shows, so the
        // negative view's generation moves with it too.
        #expect(EditModel.negativeViewGeneration(of: cropped) != EditModel.negativeViewGeneration(of: none))
    }
}

extension RollManifest.Negative {
    /// A copy with the given spots summary — `spotsSummary` is the one
    /// `var` field, so a value copy mutates it in place.
    func with(spotsSummary: NegativeSpots.Summary?) -> RollManifest.Negative {
        var copy = self
        copy.spotsSummary = spotsSummary
        return copy
    }
}

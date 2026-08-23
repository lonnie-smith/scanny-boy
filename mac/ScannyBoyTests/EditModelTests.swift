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
        applied: String?
    ) -> String {
        let sequenceJSON = sequence.map(String.init) ?? "null"
        let intendedJSON = intended.map { "\"\($0)\"" } ?? "null"
        let appliedJSON = applied.map { "\"\($0)\"" } ?? "null"
        return """
            {"negative_id":"\(negativeID)","run_id":"r","sequence":\(sequenceJSON),\
            "members":["a.NEF"],\
            "expected_output":"\(negativeID).tif","status":"\(status)",\
            "output":{"name":"\(negativeID).tif","size":1,"sha256":"\(String(repeating: "a", count: 64))","width":1,"height":1},\
            "frames":[],"pairs":[],"global_rms_px":null,"canvas":null,"valid_rect":null,\
            "fill_color":[0,0,0],"rebate_deviation_px":null,"error_code":null,"error_message":null,\
            "capture_time":{"source_datetime_original":null,\
            "intended_datetime_original":\(intendedJSON),\
            "applied_datetime_original":\(appliedJSON),"date_override":null}}
            """
    }

    private static func rollInfoEvent(negatives: [String]) -> String {
        let manifest = """
            {"manifest_format_version":3,"manifest_kind":"roll","scanny_boy_version":"0.3.0",\
            "roll_id":"roll-1","roll_name":"Test Roll","shots_per_negative":3,\
            "created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z",\
            "processing_params":{},\
            "icc_profile":{"name":"x.icc","sha256":"\(String(repeating: "b", count: 64))"},\
            "stitch_params":{},"runs":[],"sources":[],\
            "negatives":[\(negatives.joined(separator: ","))],\
            "metadata":{"roll_capture_date":null,"last_applied_at":null}}
            """
        return #"{"protocol_version":4,"event":"roll_info","manifest":\#(manifest)}"#
    }

    @Test("Dirty count reflects intended versus applied, not just completion")
    func testDirtyCountReflectsIntendedVersusApplied() async throws {
        let dirty = Self.negativeJSON(
            negativeID: "n1", sequence: 1,
            intended: "2026-08-02T12:00:00", applied: "2026-08-01T12:00:00"
        )
        let clean = Self.negativeJSON(
            negativeID: "n2", sequence: 2,
            intended: "2026-08-02T12:00:01", applied: "2026-08-02T12:00:01"
        )
        let runner = try Self.isolatedRunner(rollInfoLines: [Self.rollInfoEvent(negatives: [dirty, clean])])
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        #expect(model.dirtyCount == 1)
        #expect(model.dirtyNegatives.map(\.negativeID) == ["n1"])
    }

    @Test("Apply is disabled when nothing is dirty")
    func testApplyIsDisabledWhenNothingIsDirty() async throws {
        let clean = Self.negativeJSON(
            negativeID: "n1", sequence: 1,
            intended: "2026-08-02T12:00:00", applied: "2026-08-02T12:00:00"
        )
        let runner = try Self.isolatedRunner(rollInfoLines: [Self.rollInfoEvent(negatives: [clean])])
        let model = EditModel(runner: runner)

        model.rollURL = URL(filePath: "/tmp/roll")
        await model.waitForPendingFetch()

        #expect(model.dirtyCount == 0)
        #expect(!model.canApply)
        #expect(model.applyCommand == nil)
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
        #expect(model.dirtyCount == 2)
    }
}

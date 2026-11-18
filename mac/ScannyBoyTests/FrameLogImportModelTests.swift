import Foundation
import Testing

@testable import ScannyBoy

@MainActor
private final class ImportedManifestBox {
    var manifest: RollManifest?
}

/// The import model against a fake helper: the roll list, the alignment
/// fetch, the apply, and the token's absence.
@Suite("Frame log import model")
@MainActor
struct FrameLogImportModelTests {
    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static let token = "fl_abcdefgh0123456789"
    private static let rollURL = URL(filePath: "/tmp/roll")

    private static func isolatedDefaults() -> UserDefaults {
        UserDefaults(suiteName: "scanny-boy-tests-\(UUID().uuidString)")!
    }

    private static let rollsLine = TestEvents.line(
        #"{"event":"framelog_rolls","rolls":[{"uid":"aaaaaaaaaa","roll_number":41,"frame_count":20,"last_frame":20},{"uid":"bbbbbbbbbb","roll_number":42,"frame_count":3,"last_frame":3}]}"#
    )

    private static let rollLine = TestEvents.line(
        #"{"event":"framelog_roll","roll":{"uid":"bbbbbbbbbb","roll_number":42,"frame_count":3,"last_frame":3,"missing_frame_numbers":[]},"frames":[{"uid":"f1","frame_number":1},{"uid":"f2","frame_number":2},{"uid":"f3","frame_number":3}],"alignment":[{"negative_id":"n1","frame_number":1},{"negative_id":"n2","frame_number":2}],"linked":false}"#
    )

    private static let manifestJSON =
        #"{"manifest_format_version":5,"manifest_kind":"roll","scanny_boy_version":"0.3.0","roll_id":"roll-1","roll_name":"Test Roll","created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z","processing_params":{},"icc_profile":{"name":"x.icc","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"stitch_params":{},"runs":[],"sources":[],"negatives":[],"metadata":{"roll_capture_date":null,"last_applied_at":null,"film":null,"iso":null,"city":null,"state":null,"camera":null,"lens":null,"caption":null,"framelog_roll_uid":"bbbbbbbbbb","framelog_roll_number":42}}"#

    private static func appliedLine() -> String {
        TestEvents.line(#"{"event":"metadata_updated","manifest":\#(manifestJSON)}"#)
    }

    private static let finished = TestEvents.line(
        #"{"event":"finished","status":"success","exit_status":0}"#
    )

    private static func makeModel(
        directory: URL,
        tokens: InMemoryFrameLogTokenStore,
        box: ImportedManifestBox
    ) throws -> FrameLogImportModel {
        let script = """
            if [ "$1" = "framelog" ] && [ "$2" = "rolls" ]; then
            echo '\(rollsLine)'
            fi
            if [ "$1" = "framelog" ] && [ "$2" = "roll" ]; then
            echo '\(rollLine)'
            fi
            if [ "$1" = "framelog" ] && [ "$2" = "apply" ]; then
            echo '\(appliedLine())'
            fi
            echo '\(finished)'
            """
        let executable = try TestSupport.writeTestExecutable(script, in: directory)
        return FrameLogImportModel(
            runner: CLIRunner(executable: executable),
            rollURL: rollURL,
            negativeCount: 2,
            linkedRollUID: "bbbbbbbbbb",
            tokens: tokens,
            settings: FrameLogSettings(defaults: isolatedDefaults()),
            onImported: { manifest in box.manifest = manifest }
        )
    }

    @Test("the roll list loads newest first with the linked roll preselected")
    func loadsRolls() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = try Self.makeModel(
            directory: directory,
            tokens: InMemoryFrameLogTokenStore(token: Self.token),
            box: ImportedManifestBox()
        )
        await model.loadRolls()
        #expect(model.phase == .choosingRoll)
        #expect(model.rolls.map(\.rollNumber) == [42, 41])
        #expect(model.selectedRollUID == "bbbbbbbbbb")
        let first = try #require(model.rolls.first)
        let second = try #require(model.rolls.last)
        #expect(model.isLikelyMatch(first))
        #expect(!model.isLikelyMatch(second))
    }

    @Test("choosing a roll builds the alignment, and apply hands the manifest over")
    func choosesAndApplies() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let box = ImportedManifestBox()
        let model = try Self.makeModel(
            directory: directory,
            tokens: InMemoryFrameLogTokenStore(token: Self.token),
            box: box
        )
        await model.loadRolls()
        let roll = try #require(model.selectedRoll)
        await model.choose(roll: roll)
        #expect(model.phase == .aligning)
        #expect(model.alignment?.matchedCount == 2)
        #expect(model.framesByNumber.count == 3)

        model.setFrame(row: 0, to: 2)
        #expect(model.alignment?.rows.map(\.frameNumber) == [2, 3])

        await model.apply()
        #expect(model.didImport)
        #expect(box.manifest?.metadata.framelogRollUID == "bbbbbbbbbb")
        #expect(box.manifest?.metadata.framelogRollNumber == 42)
    }

    @Test("without a token the model fails before running anything")
    func noToken() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let model = try Self.makeModel(
            directory: directory,
            tokens: InMemoryFrameLogTokenStore(),
            box: ImportedManifestBox()
        )
        await model.loadRolls()
        #expect(model.phase == .failed(FrameLogImportModel.noTokenMessage))
        model.acknowledgeFailure()
        #expect(model.phase == .choosingRoll)
    }
}

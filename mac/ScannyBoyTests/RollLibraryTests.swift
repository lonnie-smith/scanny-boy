import Foundation
import Testing

@testable import ScannyBoy

/// Drives `RollLibrary` against fake CLI executables, in the same style as
/// `RunModelTests` and `ConfigurationModelTests`: a `/bin/sh` script is the
/// cheapest thing that can emit exactly the events each case needs. No test
/// here touches the real bundled helper — that is exercised by
/// `CLIIntegrationTests` and by the CLI's own suite.
///
/// Section 3.1's rule is the point of this whole suite: `RollLibrary` does
/// no directory enumeration and no manifest parsing of its own. Every test
/// injects `libraryBase` explicitly — never `.picturesDirectory` (section
/// 4's test rule).
@Suite("Roll library (Chunk P3-10)")
@MainActor
struct RollLibraryTests {
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

    private static func echoLines(_ lines: [String]) -> String {
        lines.map { "echo '\($0.replacingOccurrences(of: "'", with: "'\\''"))'" }
            .joined(separator: "\n")
    }

    /// A fake `scanny-boy` that answers `roll list` from `listLines`,
    /// `roll init` from `initLines`, `roll rename` from `renameLines`, all
    /// distinguished by `$1 $2` (`roll list`/`roll init`/`roll rename`) —
    /// the same discriminator the real CLI's own subcommands use.
    private static func fakeRollExecutable(
        in directory: URL,
        listLines: [String] = [],
        initLines: [String] = [],
        renameLines: [String] = []
    ) throws -> URL {
        let script = """
            if [ "$1" = "roll" ] && [ "$2" = "list" ]; then
            \(echoLines(listLines))
            exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "init" ]; then
            \(echoLines(initLines))
            exit 0
            fi
            if [ "$1" = "roll" ] && [ "$2" = "rename" ]; then
            \(echoLines(renameLines))
            exit 0
            fi
            exit 1
            """
        return try TestSupport.writeTestExecutable(script, in: directory)
    }

    private static func rollListEvent(entries: [String]) -> String {
        #"{"protocol_version":3,"event":"roll_list","rolls":[\#(entries.joined(separator: ","))]}"#
    }

    private static func listingEntry(
        path: URL,
        status: String = "ok",
        reason: String = "null",
        rollID: String? = "roll-1",
        rollName: String? = "Roll A",
        negativeCount: Int? = 2
    ) -> String {
        let idField = rollID.map { "\"\($0)\"" } ?? "null"
        let nameField = rollName.map { "\"\($0)\"" } ?? "null"
        let countField = negativeCount.map(String.init) ?? "null"
        return #"{"path":"\#(path.path)","status":"\#(status)","reason":\#(reason),"roll_id":\#(idField),"roll_name":\#(nameField),"negative_count":\#(countField)}"#
    }

    // MARK: - Scanning

    @Test("scan() finds only what roll list reports, not what is really on disk")
    func testScanFindsOnlyDirectoriesWithAManifest() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: libraryBase, withIntermediateDirectories: true)

        // A real sibling directory that holds no roll -- and is not named in
        // the canned `roll_list` response below.
        try FileManager.default.createDirectory(
            at: libraryBase.appending(path: "not-a-roll", directoryHint: .isDirectory),
            withIntermediateDirectories: true
        )

        let rollPath = libraryBase.appending(path: "Roll-A", directoryHint: .isDirectory)
        let executable = try Self.fakeRollExecutable(
            in: directory,
            listLines: [Self.rollListEvent(entries: [Self.listingEntry(path: rollPath)])]
        )

        let library = RollLibrary(
            runner: CLIRunner(executable: executable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )
        library.scan()
        await library.waitForScan()

        #expect(library.rolls.count == 1)
        #expect(library.rolls.first?.path.path == rollPath.path)
    }

    @Test("An unreadable roll is listed with its reason")
    func testUnreadableRollIsListedWithItsReason() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)

        let rollPath = libraryBase.appending(path: "broken-roll", directoryHint: .isDirectory)
        let entry = Self.listingEntry(
            path: rollPath,
            status: "unreadable",
            reason: #"{"code":"BAD_MANIFEST","message":"not valid JSON"}"#,
            rollID: nil,
            rollName: nil,
            negativeCount: nil
        )
        let executable = try Self.fakeRollExecutable(
            in: directory,
            listLines: [Self.rollListEvent(entries: [entry])]
        )

        let library = RollLibrary(
            runner: CLIRunner(executable: executable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )
        library.scan()
        await library.waitForScan()

        let roll = try #require(library.rolls.first)
        #expect(roll.status == .unreadable)
        #expect(roll.reason?.code == .badManifest)
        #expect(roll.reason?.message == "not valid JSON")
        #expect(roll.rollID == nil)
        #expect(roll.displayName == "broken-roll")
    }

    @Test("The sidebar is built from roll list alone, never by enumerating the library")
    func testLibraryIsBuiltFromRollListWithoutEnumeratingTheFilesystem() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: libraryBase, withIntermediateDirectories: true)

        // Two *real* roll folders on disk, each with a real manifest file --
        // and neither named in the canned `roll_list` response. If
        // `RollLibrary` ever enumerated the filesystem itself, these would
        // show up; the whole point of section 3.1 is that they must not.
        for name in ["Real-Roll-One", "Real-Roll-Two"] {
            let rollDir = libraryBase.appending(path: name, directoryHint: .isDirectory)
            try FileManager.default.createDirectory(at: rollDir, withIntermediateDirectories: true)
            try "{}".write(
                to: rollDir.appending(path: "scanny-boy-roll.json", directoryHint: .notDirectory),
                atomically: true, encoding: .utf8
            )
        }

        let reportedPath = libraryBase.appending(path: "Reported-Roll", directoryHint: .isDirectory)
        let executable = try Self.fakeRollExecutable(
            in: directory,
            listLines: [Self.rollListEvent(entries: [Self.listingEntry(path: reportedPath)])]
        )

        let library = RollLibrary(
            runner: CLIRunner(executable: executable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )
        library.scan()
        await library.waitForScan()

        #expect(library.rolls.map { $0.path.path } == [reportedPath.path])
    }

    // MARK: - Create

    @Test("createRoll issues roll init and returns the created roll")
    func testCreateRollIssuesRollInit() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)

        let rollPath = libraryBase.appending(path: "New-Roll", directoryHint: .isDirectory)
        let executable = try Self.fakeRollExecutable(
            in: directory,
            initLines: [
                #"{"protocol_version":3,"event":"roll_created","roll_id":"id-9","roll_name":"New Roll","path":"\#(rollPath.path)"}"#
            ]
        )

        let library = RollLibrary(
            runner: CLIRunner(executable: executable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )

        let result = await library.createRoll(name: "New Roll", shotsPerNegative: 3)
        guard case .success(let roll) = result else {
            Issue.record("expected a created roll, got \(result)")
            return
        }
        #expect(roll.path.path == rollPath.path)
        #expect(roll.rollName == "New Roll")
    }

    // MARK: - Rename

    @Test("renameRoll moves the folder (via roll rename) and updates the name")
    func testRenameMovesTheFolder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)

        let oldPath = libraryBase.appending(path: "Old-Name", directoryHint: .isDirectory)
        let newPath = libraryBase.appending(path: "New-Name", directoryHint: .isDirectory)
        let executable = try Self.fakeRollExecutable(
            in: directory,
            renameLines: [
                #"{"protocol_version":3,"event":"roll_renamed","roll_id":"id-1","roll_name":"New Name","path":"\#(newPath.path)"}"#
            ]
        )

        let library = RollLibrary(
            runner: CLIRunner(executable: executable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )
        let oldRoll = Roll(
            path: oldPath, status: .ok, reason: nil,
            rollID: "id-1", rollName: "Old Name", negativeCount: 0
        )

        let renamed = try await library.renameRoll(oldRoll, to: "New Name", runIsActive: false)

        #expect(renamed.path.path == newPath.path)
        #expect(renamed.rollName == "New Name")
    }

    @Test("renameRoll is refused while a run is active, without calling the CLI")
    func testRenameIsRefusedDuringARun() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)

        // An executable that does not exist: if `renameRoll` refuses before
        // ever building the CLI command, this is never reached, so any error
        // other than `.runInProgress` means the guard did not fire first.
        let missingExecutable = directory.appending(path: "does-not-exist")
        let library = RollLibrary(
            runner: CLIRunner(executable: missingExecutable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )
        let roll = Roll(
            path: libraryBase.appending(path: "Roll-A", directoryHint: .isDirectory),
            status: .ok, reason: nil, rollID: "id-1", rollName: "Roll A", negativeCount: 0
        )

        do {
            _ = try await library.renameRoll(roll, to: "New Name", runIsActive: true)
            Issue.record("expected renameRoll to throw")
        } catch RollLibrary.RenameError.runInProgress {
            // Expected.
        } catch {
            Issue.record("expected .runInProgress, got \(error)")
        }
    }

    // MARK: - Delete

    @Test("deleteRoll moves the folder to the Trash")
    func testDeleteRecyclesTheFolder() async throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let libraryBase = directory.appending(path: "library", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: libraryBase, withIntermediateDirectories: true)

        let rollPath = libraryBase.appending(path: "Roll-To-Delete", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: rollPath, withIntermediateDirectories: true)
        try "{}".write(
            to: rollPath.appending(path: "scanny-boy-roll.json", directoryHint: .notDirectory),
            atomically: true, encoding: .utf8
        )

        let executable = try Self.fakeRollExecutable(
            in: directory,
            listLines: [Self.rollListEvent(entries: [])]
        )
        let library = RollLibrary(
            runner: CLIRunner(executable: executable),
            libraryBase: libraryBase,
            defaults: Self.isolatedDefaults()
        )
        let roll = Roll(
            path: rollPath, status: .ok, reason: nil,
            rollID: "id-1", rollName: "Roll To Delete", negativeCount: 0
        )

        try await library.deleteRoll(roll)

        #expect(!FileManager.default.fileExists(atPath: rollPath.path))
    }
}

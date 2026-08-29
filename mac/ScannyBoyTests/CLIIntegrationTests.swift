import Foundation
import Testing

@testable import ScannyBoy

/// End-to-end runs of the real bundled helper against the real sample NEFs.
///
/// These need both the built app (for the nested helper) and the sample files
/// at `tests/fixtures/nef/`, which are ignored by Git and absent from CI, so
/// they are skipped there with a reason that says what went untested.
@Suite("Bundled helper, end to end")
struct CLIIntegrationTests {
    private static var canRun: Bool { HostBundle.isAvailable && SampleFixtures.areAvailable }

    /// Names only the prerequisite that is actually missing, so a CI skip
    /// does not also claim the app was not built.
    private static var unavailable: Comment {
        var reasons: [String] = []
        if !HostBundle.isAvailable {
            reasons.append(HostBundle.unavailableComment.rawValue)
        }
        if !SampleFixtures.areAvailable {
            reasons.append(SampleFixtures.unavailableComment.rawValue)
        }
        return Comment(rawValue: reasons.joined(separator: "\n"))
    }

    private static func runner() throws -> CLIRunner {
        CLIRunner(executable: try #require(HostBundle.helperExecutableURL))
    }

    private static func stagingDirectories(in folder: URL) throws -> [URL] {
        try FileManager.default
            .contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasSuffix(".scanny-staging") }
            .sorted { $0.path < $1.path }
    }

    private static func manifestStatus(in folder: URL) throws -> String? {
        let url = folder.appending(path: "scanny-boy-manifest.json")
        let data = try Data(contentsOf: url)
        let json = try JSONDecoder().decode(JSONValue.self, from: data)
        return json.objectValue?["status"]?.stringValue
    }

    @Test(
        "probe returns the catalogue in canonical order",
        .enabled(if: CLIIntegrationTests.canRun, CLIIntegrationTests.unavailable)
    )
    func probeReturnsTheCatalogue() async throws {
        let session = try Self.runner().session(
            for: .probe(input: SampleFixtures.directory)
        )
        let collected = await TestSupport.drain(try await session.start())

        #expect(collected.failures.isEmpty)
        let result = try #require(collected.events.first { $0.kind == .probeResult })
        #expect(result.catalogue == SampleFixtures.files)
        #expect(collected.terminalCompletion?.outcome == .success)
    }

    @Test(
        "probe with a selection reports two negatives of three frames",
        .enabled(if: CLIIntegrationTests.canRun, CLIIntegrationTests.unavailable)
    )
    func probeWithSelectionReportsGroups() async throws {
        let session = try Self.runner().session(
            for: .probe(
                input: SampleFixtures.directory,
                files: SampleFixtures.files,
                perNegative: 3
            )
        )
        let collected = await TestSupport.drain(try await session.start())

        let result = try #require(collected.events.first { $0.kind == .probeResult })
        #expect(result.groups == [Array(SampleFixtures.files[0..<3]), Array(SampleFixtures.files[3..<6])])
        #expect(collected.terminalCompletion?.outcome == .success)
    }

    @Test(
        "a usage error from the real helper is classified as one",
        .enabled(if: CLIIntegrationTests.canRun, CLIIntegrationTests.unavailable)
    )
    func realUsageErrorIsClassified() async throws {
        let session = try Self.runner().session(
            for: CLICommand(arguments: ["probe", "--nonsense"])
        )
        let collected = await TestSupport.drain(try await session.start())
        #expect(collected.terminalCompletion?.outcome == .usageError)
    }

    /// Section 3.8: "A forced stop cannot clean files, update the manifest, or
    /// emit a final event... The next probe or conversion detects a manifest
    /// left as `running` and staging directories owned by that run. It removes
    /// those staging directories before rerunning."
    ///
    /// Driven entirely through `CLISession`, so what is under test is the
    /// Swift side's forced-cancel classification alongside the recovery it
    /// leaves the next run to perform.
    @Test(
        "a forced stop leaves a running manifest that the next run recovers",
        .enabled(if: CLIIntegrationTests.canRun, CLIIntegrationTests.unavailable),
        .timeLimit(.minutes(5))
    )
    func forcedStopIsRecoveredByTheNextRun() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let out = directory.appending(path: "out", directoryHint: .isDirectory)
            try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

            let runner = try Self.runner()
            let command = CLICommand.convert(
                input: SampleFixtures.directory,
                files: SampleFixtures.files,
                out: out,
                filmDate: "2026-08-02",
                perNegative: 3,
                jobs: 2
            )

            // First run: killed outright once conversion is genuinely under
            // way, with no chance to clean up after itself.
            let first = runner.session(for: command)
            var killed = false
            var collected: [CLISessionOutput] = []
            for await output in try await first.start() {
                collected.append(output)
                if !killed, case .event(let event) = output, event.kind == .progress {
                    killed = true
                    await first.forceTerminate()
                }
            }
            #expect(killed, "the helper never reported progress, so nothing was force-stopped")

            let completion = try #require(collected.terminalCompletion)
            #expect(completion.outcome == .cancelled(forced: true))
            #expect(completion.terminationReason == .uncaughtSignal)
            #expect(completion.terminationStatus == SIGKILL)

            // Exactly the wreckage section 3.8 predicts.
            #expect(try Self.manifestStatus(in: out) == "running")
            #expect(try Self.stagingDirectories(in: out).count == 1)

            // Second run: cleans it up and finishes the roll.
            let second = runner.session(for: command)
            let recovered = await TestSupport.drain(try await second.start())

            #expect(recovered.failures.isEmpty)
            #expect(recovered.terminalCompletion?.outcome == .success)
            let finished = try #require(recovered.events.last)
            #expect(finished.kind == .finished)
            // The event's own vocabulary: `success`, where the manifest
            // written beside it says `complete`.
            #expect(finished.status == "success")
            #expect(finished.exitStatus == 0)

            #expect(try Self.manifestStatus(in: out) == "complete")
            #expect(try Self.stagingDirectories(in: out).isEmpty)
            for name in SampleFixtures.files {
                let tiff = out.appending(path: (name as NSString).deletingPathExtension + ".tif")
                #expect(
                    FileManager.default.fileExists(atPath: tiff.path),
                    "the rerun did not publish \(tiff.lastPathComponent)"
                )
            }
        }
    }
}

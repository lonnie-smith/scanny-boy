import Foundation
import Testing

@testable import ScannyBoy

/// The argument lists must match `shared/contract/CONTRACT.md` exactly; the
/// CLI rejects anything else as a usage error.
@Suite("CLI command construction")
struct CLICommandTests {
    private static let input = URL(filePath: "/Volumes/Scans/roll-12")
    private static let out = URL(filePath: "/Volumes/Scans/roll-12-tif")

    @Test("probe with an input folder alone asks only for the catalogue")
    func probeWithInputAlone() {
        let command = CLICommand.probe(input: Self.input)
        #expect(command.arguments == ["probe", "--input", "/Volumes/Scans/roll-12"])
    }

    @Test("probe with a selection passes relative filenames, never paths")
    func probeWithSelection() {
        let command = CLICommand.probe(
            input: Self.input,
            files: ["_DSC4638.NEF", "_DSC4639.NEF"],
            perNegative: 3
        )
        #expect(
            command.arguments == [
                "probe", "--input", "/Volumes/Scans/roll-12",
                "--files", "_DSC4638.NEF", "_DSC4639.NEF",
                "--per-negative", "3",
            ]
        )
        #expect(!command.arguments.contains { $0.hasPrefix("/Volumes/Scans/roll-12/_DSC") })
    }

    @Test("probe can include the output folder for the conflict preview")
    func probeWithOutputFolder() {
        let command = CLICommand.probe(input: Self.input, files: ["a.NEF"], out: Self.out)
        #expect(
            command.arguments == [
                "probe", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--out", "/Volumes/Scans/roll-12-tif",
            ]
        )
    }

    @Test("probe can include a roll folder for the overlap preview")
    func probeWithRoll() {
        let command = CLICommand.probe(input: Self.input, files: ["a.NEF"], roll: Self.out)
        #expect(
            command.arguments == [
                "probe", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--roll", "/Volumes/Scans/roll-12-tif",
            ]
        )
    }

    @Test("convert carries every required flag")
    func convertRequiredFlags() {
        let command = CLICommand.convert(
            input: Self.input,
            files: ["a.NEF", "b.NEF", "c.NEF"],
            out: Self.out,
            filmDate: "2026-08-02"
        )
        #expect(
            command.arguments == [
                "convert", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF", "b.NEF", "c.NEF",
                "--out", "/Volumes/Scans/roll-12-tif",
                "--film-date", "2026-08-02",
            ]
        )
    }

    @Test("convert adds the optional flags only when asked")
    func convertOptionalFlags() {
        let command = CLICommand.convert(
            input: Self.input,
            files: ["a.NEF"],
            out: Self.out,
            filmDate: "2026-08-02",
            perNegative: 1,
            jobs: 4,
            overwrite: true
        )
        #expect(
            command.arguments == [
                "convert", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--out", "/Volumes/Scans/roll-12-tif",
                "--film-date", "2026-08-02",
                "--per-negative", "1",
                "--jobs", "4",
                "--overwrite",
            ]
        )
    }

    @Test("--overwrite is absent unless the user confirmed the replacements")
    func overwriteIsOptIn() {
        let command = CLICommand.convert(
            input: Self.input,
            files: ["a.NEF"],
            out: Self.out,
            filmDate: "2026-08-02"
        )
        #expect(!command.arguments.contains("--overwrite"))
    }

    @Test("run carries every required flag")
    func runRequiredFlags() {
        let command = CLICommand.run(
            input: Self.input,
            files: ["a.NEF", "b.NEF", "c.NEF"],
            roll: Self.out
        )
        #expect(
            command.arguments == [
                "run", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF", "b.NEF", "c.NEF",
                "--roll", "/Volumes/Scans/roll-12-tif",
            ]
        )
    }

    @Test("run adds the optional flags only when asked")
    func runOptionalFlags() {
        let work = URL(filePath: "/Volumes/Scans/roll-12-work")
        let command = CLICommand.run(
            input: Self.input,
            files: ["a.NEF"],
            roll: Self.out,
            perNegative: 1,
            jobs: 4,
            skipSources: ["a.NEF"],
            work: work
        )
        #expect(
            command.arguments == [
                "run", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--per-negative", "1",
                "--jobs", "4",
                "--work", "/Volumes/Scans/roll-12-work",
                "--skip-sources", "a.NEF",
            ]
        )
    }

    @Test("run omits --work, --keep-intermediates, and --skip-sources unless given")
    func runWithoutWorkOrKeepIntermediates() {
        let command = CLICommand.run(
            input: Self.input,
            files: ["a.NEF"],
            roll: Self.out
        )
        #expect(!command.arguments.contains("--work"))
        #expect(!command.arguments.contains("--keep-intermediates"))
        #expect(!command.arguments.contains("--skip-sources"))
        #expect(!command.arguments.contains("--overwrite"))
        #expect(!command.arguments.contains("--film-date"))
    }

    // MARK: - Chunk P2-10's additions

    private static let work = URL(filePath: "/Volumes/Scans/roll-12-work")

    @Test("stitch carries every required flag, and defaults to --allow-partial")
    func stitchRequiredFlags() {
        let command = CLICommand.stitch(work: Self.work, roll: Self.out)
        #expect(
            command.arguments == [
                "stitch", "--work", "/Volumes/Scans/roll-12-work",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--allow-partial",
            ]
        )
    }

    @Test("stitch adds --jobs and --overwrite only when asked")
    func stitchOptionalFlags() {
        let command = CLICommand.stitch(work: Self.work, roll: Self.out, jobs: 4, overwrite: true)
        #expect(
            command.arguments == [
                "stitch", "--work", "/Volumes/Scans/roll-12-work",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--jobs", "4",
                "--overwrite",
                "--allow-partial",
            ]
        )
    }

    @Test("stitch omits --allow-partial when explicitly turned off")
    func stitchAllowPartialOptOut() {
        let command = CLICommand.stitch(work: Self.work, roll: Self.out, allowPartial: false)
        #expect(!command.arguments.contains("--allow-partial"))
    }
}

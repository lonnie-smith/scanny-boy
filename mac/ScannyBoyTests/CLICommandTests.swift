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
            across: 3
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

    // MARK: - Grid grouping emission (protocol 10)

    @Test("down > 1 emits --grid AxD, not --per-negative")
    func gridEmittedWhenDownAboveOne() {
        let probe = CLICommand.probe(input: Self.input, files: ["a.NEF"], across: 3, down: 2)
        #expect(probe.arguments.contains("--grid"))
        #expect(probe.arguments.contains("3x2"))
        #expect(!probe.arguments.contains("--per-negative"))

        let run = CLICommand.run(
            input: Self.input, files: ["a.NEF"], roll: Self.out, across: 5, down: 2
        )
        #expect(run.arguments.contains("--grid"))
        #expect(run.arguments.contains("5x2"))
        #expect(!run.arguments.contains("--per-negative"))

        let prepare = CLICommand.prepare(
            input: Self.input, files: ["a.NEF"], out: Self.out, across: 6, down: 2
        )
        #expect(prepare.arguments.contains("--grid"))
        #expect(prepare.arguments.contains("6x2"))
        #expect(!prepare.arguments.contains("--per-negative"))
    }

    @Test("down == 1 keeps the strip command byte-identical: --per-negative N")
    func stripRunStaysPerNegative() throws {
        let run = CLICommand.run(
            input: Self.input, files: ["a.NEF"], roll: Self.out, across: 3, down: 1
        )
        let index = try #require(run.arguments.firstIndex(of: "--per-negative"))
        #expect(run.arguments[index + 1] == "3")
        #expect(!run.arguments.contains("--grid"))
    }

    @Test("no across chosen emits neither grouping flag")
    func noGroupingFlagsWithoutAcross() {
        let run = CLICommand.run(
            input: Self.input, files: ["a.NEF"], roll: Self.out
        )
        #expect(!run.arguments.contains("--per-negative"))
        #expect(!run.arguments.contains("--grid"))
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
        let command = CLICommand.prepare(
            input: Self.input,
            files: ["a.NEF", "b.NEF", "c.NEF"],
            out: Self.out
        )
        #expect(
            command.arguments == [
        "prepare", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF", "b.NEF", "c.NEF",
                "--out", "/Volumes/Scans/roll-12-tif",
            ]
        )
    }

    @Test("convert adds the optional flags only when asked")
    func convertOptionalFlags() {
        let command = CLICommand.prepare(
            input: Self.input,
            files: ["a.NEF"],
            out: Self.out,
            across: 1,
            jobs: 4,
            overwrite: true
        )
        #expect(
            command.arguments == [
        "prepare", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--out", "/Volumes/Scans/roll-12-tif",
                "--per-negative", "1",
                "--jobs", "4",
                "--overwrite",
            ]
        )
    }

    @Test("--overwrite is absent unless the user confirmed the replacements")
    func overwriteIsOptIn() {
        let command = CLICommand.prepare(
            input: Self.input,
            files: ["a.NEF"],
            out: Self.out
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
            across: 1,
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

    @Test("edit delete names the roll and the negative")
    func editDeleteArguments() {
        let command = CLICommand.editDelete(roll: Self.out, negatives: ["a1b2c3-negative-01"])
        #expect(
            command.arguments == [
                "edit", "delete",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "a1b2c3-negative-01",
            ]
        )
    }

    // MARK: - Edit flip and batch selection

    @Test("edit rotate repeats --negative for each selected frame")
    func editRotateSelectionArguments() {
        let command = CLICommand.editRotate(
            roll: Self.out,
            negatives: ["neg-01", "neg-02"],
            clockwise: false
        )
        #expect(
            command.arguments == [
                "edit", "rotate",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "neg-01",
                "--negative", "neg-02",
                "--direction", "ccw",
            ]
        )
    }

    @Test("edit flip repeats --negative for each selected frame")
    func editFlipSelectionArguments() {
        let command = CLICommand.editFlip(roll: Self.out, negatives: ["neg-01"])
        #expect(
            command.arguments == [
                "edit", "flip",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "neg-01",
            ]
        )
    }

    @Test("edit delete repeats --negative for each selected frame")
    func editDeleteSelectionArguments() {
        let command = CLICommand.editDelete(
            roll: Self.out, negatives: ["neg-01", "neg-02", "neg-03"]
        )
        #expect(
            command.arguments == [
                "edit", "delete",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "neg-01",
                "--negative", "neg-02",
                "--negative", "neg-03",
            ]
        )
    }

    // MARK: - Protocol version 6: flat field

    @Test("run carries --flatfield when a profile is chosen")
    func runWithFlatField() {
        let command = CLICommand.run(
            input: Self.input,
            files: ["a.NEF"],
            roll: Self.out,
            flatfield: "pid-1"
        )
        #expect(
            command.arguments == [
                "run", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--flatfield", "pid-1",
            ]
        )
    }

    @Test("run omits --flatfield when no profile is chosen")
    func runWithoutFlatField() {
        let command = CLICommand.run(input: Self.input, files: ["a.NEF"], roll: Self.out)
        #expect(!command.arguments.contains("--flatfield"))
    }

    @Test("probe carries --flatfield when a profile is chosen")
    func probeWithFlatField() throws {
        let command = CLICommand.probe(
            input: Self.input,
            files: ["a.NEF"],
            roll: Self.out,
            across: 3,
            flatfield: "pid-1"
        )
        #expect(command.arguments.contains("--flatfield"))
        let index = try #require(command.arguments.firstIndex(of: "--flatfield"))
        #expect(command.arguments[index + 1] == "pid-1")
    }

    @Test("flatfield create names the reference and the profile")
    func flatfieldCreateArguments() {
        let reference = URL(filePath: "/Volumes/Refs/bare-light.NEF")
        let command = CLICommand.flatfieldCreate(reference: reference, name: "Copy stand")
        #expect(
            command.arguments == [
                "flatfield", "create",
                "--reference", "/Volumes/Refs/bare-light.NEF",
                "--name", "Copy stand",
            ]
        )
    }

    @Test("flatfield list and delete are shaped like CONTRACT.md says")
    func flatfieldListAndDeleteArguments() {
        #expect(CLICommand.flatfieldList().arguments == ["flatfield", "list"])
        #expect(
            CLICommand.flatfieldDelete(profile: "pid-1").arguments == [
                "flatfield", "delete", "--profile", "pid-1",
            ]
        )
    }
}

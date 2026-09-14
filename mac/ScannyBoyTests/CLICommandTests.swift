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

    // MARK: - Edit crop (protocol 19)

    @Test("edit crop carries the display-space rect, tilt, and preset")
    func editCropArguments() {
        let command = CLICommand.editCrop(
            roll: Self.out,
            negative: "neg-01",
            rect: CGRect(x: 10, y: 8, width: 50, height: 24),
            tiltDegrees: 2.5,
            preset: "35mm"
        )
        #expect(
            command.arguments == [
                "edit", "crop",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "neg-01",
                "--x", "10",
                "--y", "8",
                "--width", "50",
                "--height", "24",
                "--tilt", "2.5",
                "--preset", "35mm",
            ]
        )
    }

    @Test("edit crop can name the full uncropped display canvas")
    func editCropFullFrameArguments() {
        let command = CLICommand.editCrop(
            roll: Self.out,
            negative: "neg-01",
            rect: CGRect(x: 10, y: 8, width: 50, height: 24),
            tiltDegrees: 2.5,
            preset: "35mm",
            fullFrame: true
        )
        #expect(command.arguments.contains("--full-frame"))
    }

    @Test("edit render-preview can ignore the live crop")
    func editRenderPreviewFullFrameArguments() {
        let command = CLICommand.editRenderPreview(
            roll: Self.out,
            negative: "neg-01",
            mode: "positive",
            output: URL(filePath: "/tmp/preview.png"),
            fullFrame: true
        )
        #expect(command.arguments.contains("--full-frame"))
    }

    @Test("edit crop without a rect is the reset")
    func editCropResetArguments() {
        let command = CLICommand.editCrop(
            roll: Self.out, negative: "neg-01", rect: nil
        )
        #expect(
            command.arguments == [
                "edit", "crop",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "neg-01",
                "--reset",
            ]
        )
    }

    // MARK: - Edit tone

    @Test("edit tone carries the full adjustment, or --reset")
    func editToneArguments() {
        let command = CLICommand.editTone(
            roll: Self.out,
            negatives: ["neg-01"],
            adjustment: ToneAdjustment(
                snapGamma: 0.2, density: 1.1, shadowDensity: 0.1,
                highlightDensity: -0.1
            )
        )
        #expect(
            command.arguments == [
                "edit", "tone",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--negative", "neg-01",
                "--snap", "0.2",
                "--density", "1.1",
                "--shadow-density", "0.1",
                "--highlight-density", "-0.1",
            ]
        )
    }

    @Test("edit tone with no adjustment is a reset")
    func editToneResetArguments() {
        let command = CLICommand.editTone(
            roll: Self.out, negatives: ["neg-01"], adjustment: nil
        )
        #expect(command.arguments.last == "--reset")
        #expect(!command.arguments.contains("--grade"))
        #expect(!command.arguments.contains("--snap"))
    }

    // MARK: - Edit color

    @Test("edit color sends warmth, tint, and curve offsets")
    func editColorBalanceArguments() {
        var adjustment = ColorAdjustment.neutral
        adjustment.warmth = 0.3
        adjustment.tint = -0.1
        adjustment.curveRed25 = 0.05
        adjustment.curveGreen50 = -0.03
        adjustment.curveBlue75 = 0.02
        let command = CLICommand.editColor(
            roll: Self.out,
            negatives: ["neg-01"],
            adjustment: adjustment
        )
        let warmth = command.arguments.firstIndex(of: "--warmth")
        #expect(warmth.map { command.arguments[$0 + 1] } == "0.3")
        let tint = command.arguments.firstIndex(of: "--tint")
        #expect(tint.map { command.arguments[$0 + 1] } == "-0.1")
        let red25 = command.arguments.firstIndex(of: "--red-25")
        #expect(red25.map { command.arguments[$0 + 1] } == "0.05")
        let green50 = command.arguments.firstIndex(of: "--green-50")
        #expect(green50.map { command.arguments[$0 + 1] } == "-0.03")
        let blue75 = command.arguments.firstIndex(of: "--blue-75")
        #expect(blue75.map { command.arguments[$0 + 1] } == "0.02")
        #expect(!command.arguments.contains("--region"))
    }

    @Test("edit color with no adjustment is a reset")
    func editColorResetArguments() {
        let command = CLICommand.editColor(
            roll: Self.out, negatives: ["neg-01"], adjustment: nil
        )
        #expect(command.arguments.last == "--reset")
        #expect(!command.arguments.contains("--warmth"))
    }

    @Test("edit color with auto balance omits warmth and tint")
    func editColorAutoBalanceArguments() {
        var adjustment = ColorAdjustment.neutral
        adjustment.warmth = 0.2
        adjustment.tint = -0.1
        let command = CLICommand.editColor(
            roll: Self.out,
            negatives: ["neg-01"],
            adjustment: adjustment,
            auto: .balance
        )
        #expect(command.arguments.contains("--auto-balance"))
        #expect(!command.arguments.contains { $0 == "--warmth" })
        #expect(!command.arguments.contains { $0 == "--tint" })
    }

    @Test("edit color emits the highlight cast removal strength")
    func editColorCastRemovalHighlightsArguments() {
        var adjustment = ColorAdjustment.neutral
        adjustment.castRemovalHighlights = 0.4
        let command = CLICommand.editColor(
            roll: Self.out,
            negatives: ["neg-01"],
            adjustment: adjustment
        )
        #expect(command.arguments.contains("--cast-removal-highlights"))
        let index = command.arguments.firstIndex(of: "--cast-removal-highlights")
        #expect(index.map { command.arguments[$0 + 1] } == "0.4")
        #expect(!command.arguments.contains("--auto-balance"))
    }

    // MARK: - Rig profiles

    @Test("run carries --rig when a profile is chosen")
    func runWithRig() {
        let command = CLICommand.run(
            input: Self.input,
            files: ["a.NEF"],
            roll: Self.out,
            rig: "pid-1"
        )
        #expect(
            command.arguments == [
                "run", "--input", "/Volumes/Scans/roll-12",
                "--files", "a.NEF",
                "--roll", "/Volumes/Scans/roll-12-tif",
                "--rig", "pid-1",
            ]
        )
    }

    @Test("run omits --rig when no profile is chosen")
    func runWithoutRig() {
        let command = CLICommand.run(input: Self.input, files: ["a.NEF"], roll: Self.out)
        #expect(!command.arguments.contains("--rig"))
    }

    @Test("probe carries --rig when a profile is chosen")
    func probeWithRig() throws {
        let command = CLICommand.probe(
            input: Self.input,
            files: ["a.NEF"],
            roll: Self.out,
            across: 3,
            rig: "pid-1"
        )
        #expect(command.arguments.contains("--rig"))
        let index = try #require(command.arguments.firstIndex(of: "--rig"))
        #expect(command.arguments[index + 1] == "pid-1")
    }

    @Test("rig create names the profile and calibration frames")
    func rigCreateArguments() {
        let frame = URL(filePath: "/Volumes/Refs/board-01.NEF")
        let command = CLICommand.rigCreate(name: "Copy stand", calibrationFrames: [frame])
        #expect(
            command.arguments == [
                "rig", "create",
                "--name", "Copy stand",
                "--calibration", "/Volumes/Refs/board-01.NEF",
            ]
        )
    }

    @Test("rig list and delete are shaped like CONTRACT.md says")
    func rigListAndDeleteArguments() {
        #expect(CLICommand.rigList().arguments == ["rig", "list"])
        #expect(
            CLICommand.rigDelete(profile: "pid-1").arguments == [
                "rig", "delete", "--profile", "pid-1",
            ]
        )
    }

    @Test("roll set-flatfield-reference passes roll, frame, and optional rig")
    func rollSetFlatFieldReferenceArguments() {
        let roll = URL(filePath: "/tmp/roll")
        let frame = URL(filePath: "/tmp/bare-light.NEF")
        #expect(
            CLICommand.rollSetFlatFieldReference(roll: roll, frame: frame).arguments == [
                "roll", "set-flatfield-reference",
                "--roll", roll.path,
                "--frame", frame.path,
            ]
        )
        #expect(
            CLICommand.rollSetFlatFieldReference(
                roll: roll, frame: frame, rig: "pid-1"
            ).arguments == [
                "roll", "set-flatfield-reference",
                "--roll", roll.path,
                "--frame", frame.path,
                "--rig", "pid-1",
            ]
        )
    }

    @Test("grid create names the preset and its dimensions")
    func gridCreateArguments() {
        let command = CLICommand.gridCreate(name: "Hasselblad", across: 4, down: 2)
        #expect(
            command.arguments == [
                "grid", "create",
                "--name", "Hasselblad",
                "--across", "4",
                "--down", "2",
            ]
        )
    }

    @Test("grid list and delete are shaped like CONTRACT.md says")
    func gridListAndDeleteArguments() {
        #expect(CLICommand.gridList().arguments == ["grid", "list"])
        #expect(
            CLICommand.gridDelete(profile: "pid-1").arguments == [
                "grid", "delete", "--profile", "pid-1",
            ]
        )
    }
    // MARK: - Spotting (protocol 13)

    private static let roll = URL(filePath: "/Volumes/Scans/roll-12")

    @Test("detect-spots repeats --negative and carries the sensitivity")
    func detectSpotsArguments() {
        let command = CLICommand.editDetectSpots(
            roll: Self.roll,
            negatives: ["n1", "n2"],
            sensitivity: 0.75
        )
        #expect(
            command.arguments == [
                "edit", "detect-spots",
                "--roll", "/Volumes/Scans/roll-12",
                "--negative", "n1",
                "--negative", "n2",
                "--sensitivity", "0.75",
            ]
        )
    }

    @Test("spots review emits repeated --reject and --accept flags")
    func spotsReviewArguments() {
        let command = CLICommand.editSpots(
            roll: Self.roll, negative: "n1", reject: [3, 7], accept: [2]
        )
        #expect(
            command.arguments == [
                "edit", "spots",
                "--roll", "/Volumes/Scans/roll-12",
                "--negative", "n1",
                "--reject", "3",
                "--reject", "7",
                "--accept", "2",
            ]
        )
        #expect(!command.arguments.contains("--repair"))
        #expect(!command.arguments.contains("--no-repair"))
    }

    @Test("spots review's repair switch, clear, and the bare form")
    func spotsRepairSwitchArguments() {
        #expect(
            CLICommand.editSpots(roll: Self.roll, negative: "n1", repair: true)
                .arguments
                == ["edit", "spots", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1", "--repair"]
        )
        #expect(
            CLICommand.editSpots(roll: Self.roll, negative: "n1", repair: false)
                .arguments
                == ["edit", "spots", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1", "--no-repair"]
        )
        #expect(
            CLICommand.editSpots(roll: Self.roll, negative: "n1", clear: true)
                .arguments
                == ["edit", "spots", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1", "--clear"]
        )
        #expect(
            CLICommand.editSpots(roll: Self.roll, negative: "n1").arguments
                == ["edit", "spots", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1"]
        )
    }

    @Test("list-spots is the pure query's two flags")
    func listSpotsArguments() {
        #expect(
            CLICommand.editListSpots(roll: Self.roll, negative: "n1").arguments
                == ["edit", "list-spots", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1"]
        )
    }

    @Test("detect-scratches and scratches on/off round-trip the CLI flags")
    func scratchesCommandArguments() {
        #expect(
            CLICommand.editDetectScratches(roll: Self.roll, negatives: ["n1", "n2"]).arguments
                == ["edit", "detect-scratches", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1", "--negative", "n2"]
        )
        #expect(
            CLICommand.editScratches(roll: Self.roll, negatives: ["n1"], enabled: true).arguments
                == ["edit", "scratches", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1", "--on"]
        )
        #expect(
            CLICommand.editScratches(roll: Self.roll, negatives: ["n1"], enabled: false).arguments
                == ["edit", "scratches", "--roll", "/Volumes/Scans/roll-12",
                    "--negative", "n1", "--off"]
        )
    }

    @Test("roll init passes library and name, with optional film kind")
    func rollInitArguments() {
        let library = URL(filePath: "/Volumes/Scans/library")
        #expect(
            CLICommand.rollInit(library: library, name: "Tri-X").arguments
                == [
                    "roll", "init",
                    "--library", "/Volumes/Scans/library",
                    "--name", "Tri-X",
                ]
        )
        #expect(
            CLICommand.rollInit(library: library, name: "Tri-X", filmKind: "monochrome")
                .arguments
                == [
                    "roll", "init",
                    "--library", "/Volumes/Scans/library",
                    "--name", "Tri-X",
                    "--film-kind", "monochrome",
                ]
        )
    }

    @Test("roll set-film-kind passes roll and film kind")
    func rollSetFilmKindArguments() {
        let roll = URL(filePath: "/Volumes/Scans/roll-12")
        #expect(
            CLICommand.rollSetFilmKind(roll: roll, filmKind: "colour").arguments
                == [
                    "roll", "set-film-kind",
                    "--roll", "/Volumes/Scans/roll-12",
                    "--film-kind", "colour",
                ]
        )
    }

    @Test("roll set-base-frame passes roll and frame")
    func rollSetBaseFrameArguments() {
        let roll = URL(filePath: "/Volumes/Scans/roll-12")
        let frame = URL(filePath: "/Volumes/Scans/_DSC5012.NEF")
        #expect(
            CLICommand.rollSetBaseFrame(roll: roll, frame: frame).arguments
                == [
                    "roll", "set-base-frame",
                    "--roll", "/Volumes/Scans/roll-12",
                    "--frame", "/Volumes/Scans/_DSC5012.NEF",
                ]
        )
    }

    // MARK: - Export downsampling

    @Test("export carries the required flags and omits --downsample by default")
    func exportRequiredFlags() {
        let command = CLICommand.export(roll: Self.roll, output: Self.out)
        #expect(
            command.arguments
                == [
                    "export", "--roll", "/Volumes/Scans/roll-12",
                    "--output", "/Volumes/Scans/roll-12-tif",
                ]
        )
    }

    @Test("export adds --downsample only when a size is chosen")
    func exportDownsampleArguments() {
        #expect(
            CLICommand.export(roll: Self.roll, output: Self.out, downsample: 6048)
                .arguments
                == [
                    "export", "--roll", "/Volumes/Scans/roll-12",
                    "--output", "/Volumes/Scans/roll-12-tif",
                    "--downsample", "6048",
                ]
        )
        #expect(
            CLICommand.export(roll: Self.roll, output: Self.out, downsample: 9072)
                .arguments
                == [
                    "export", "--roll", "/Volumes/Scans/roll-12",
                    "--output", "/Volumes/Scans/roll-12-tif",
                    "--downsample", "9072",
                ]
        )
        #expect(
            CLICommand.export(roll: Self.roll, output: Self.out, downsample: 12096)
                .arguments
                == [
                    "export", "--roll", "/Volumes/Scans/roll-12",
                    "--output", "/Volumes/Scans/roll-12-tif",
                    "--downsample", "12096",
                ]
        )
    }
}

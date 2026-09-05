import Foundation

/// The argument list for one CLI invocation.
///
/// `shared/contract/CONTRACT.md` is the source of truth for these flags.
/// `--files` takes filenames relative to `--input`, never absolute paths.
public struct CLICommand: Sendable, Hashable {
    public let arguments: [String]

    public init(arguments: [String]) {
        self.arguments = arguments
    }

    /// `scanny-boy roll init --library DIR --name NAME`
    ///
    /// A roll records no grouping of its own: scans-per-negative is each
    /// stitch batch's choice, chosen on the Add Scans stage.
    public static func rollInit(library: URL, name: String) -> CLICommand {
        CLICommand(arguments: [
            "roll", "init",
            "--library", library.path,
            "--name", name,
        ])
    }

    /// `scanny-boy roll list --library DIR`
    public static func rollList(library: URL) -> CLICommand {
        CLICommand(arguments: ["roll", "list", "--library", library.path])
    }

    /// `scanny-boy roll info --roll DIR`
    public static func rollInfo(roll: URL) -> CLICommand {
        CLICommand(arguments: ["roll", "info", "--roll", roll.path])
    }

    /// `scanny-boy roll rename --roll DIR --name NAME`
    ///
    /// Section 5.5: the CLI moves the folder and writes the new `roll_name`;
    /// it does not enforce "refused while any run is active" itself, since
    /// it is stateless between invocations — the app checks that before
    /// ever building this command.
    public static func rollRename(roll: URL, name: String) -> CLICommand {
        CLICommand(arguments: ["roll", "rename", "--roll", roll.path, "--name", name])
    }

    /// `scanny-boy roll delete --roll DIR`
    ///
    /// Unregisters the roll from the library database — the move of the
    /// folder to the Trash is Swift's, via `NSWorkspace.recycle`, and must
    /// happen first, so a failed move leaves both the folder and the
    /// registration untouched.
    public static func rollDelete(roll: URL) -> CLICommand {
        CLICommand(arguments: ["roll", "delete", "--roll", roll.path])
    }

    /// Appends the grouping flags for one CLI invocation (protocol 10's
    /// rule): `--grid AxD` whenever `down > 1`, `--per-negative N` when
    /// `down == 1` — so a strip run's command line is byte-identical to a
    /// pre-grid one. Exactly one of the two flags is emitted, matching the
    /// CLI's exactly-one-of rule on `prepare` and `run`.
    private static func groupingArguments(
        across: Int?, down: Int
    ) -> [String] {
        guard let across else { return [] }
        if down > 1 {
            return ["--grid", "\(across)x\(down)"]
        }
        return ["--per-negative", String(across)]
    }

    /// `scanny-boy probe --input DIR [--files ...] [--out DIR] [--roll DIR] [--per-negative N | --grid AxD]`
    ///
    /// With `--input` alone this returns the catalogue in canonical order.
    /// Adding `--files` also validates the selection; adding `--out` on top of
    /// that includes output-folder validation and the overwrite-conflict
    /// preview (Phase 2's rerun path). Adding `--roll` instead validates the
    /// selection against a roll's invariants and reports `roll_overlap`
    /// (Phase 3 section 3.5) — the Add Scans stage's own probe call.
    public static func probe(
        input: URL,
        files: [String] = [],
        out: URL? = nil,
        roll: URL? = nil,
        across: Int? = nil,
        down: Int = 1,
        flatfield: String? = nil
    ) -> CLICommand {
        var arguments = ["probe", "--input", input.path]
        if !files.isEmpty {
            arguments.append("--files")
            arguments.append(contentsOf: files)
        }
        if let out {
            arguments.append(contentsOf: ["--out", out.path])
        }
        if let roll {
            arguments.append(contentsOf: ["--roll", roll.path])
        }
        arguments.append(contentsOf: groupingArguments(across: across, down: down))
        if let flatfield {
            arguments.append(contentsOf: ["--flatfield", flatfield])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy prepare --input DIR --files ... --out DIR [--per-negative N] ...`
    ///
    /// There is no `--film-date`: Phase 3 removed it from every command, and
    /// the CLI derives `film_date` from the scans' own capture times
    /// (CONTRACT.md). `overwrite` is only ever set after the user has
    /// confirmed the replacements (section 3.6).
    public static func prepare(
        input: URL,
        files: [String],
        out: URL,
        across: Int? = nil,
        down: Int = 1,
        jobs: Int? = nil,
        overwrite: Bool = false,
        flatfield: String? = nil
    ) -> CLICommand {
        var arguments = ["prepare", "--input", input.path]
        arguments.append("--files")
        arguments.append(contentsOf: files)
        arguments.append(contentsOf: ["--out", out.path])
        arguments.append(contentsOf: groupingArguments(across: across, down: down))
        if let jobs {
            arguments.append(contentsOf: ["--jobs", String(jobs)])
        }
        if overwrite {
            arguments.append("--overwrite")
        }
        if let flatfield {
            arguments.append(contentsOf: ["--flatfield", flatfield])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy run --input DIR --files ... --roll DIR [--skip-sources ...] [--work DIR]`
    ///
    /// One process, one event stream, one cancellation, from a selection of
    /// NEFs all the way to finished, stitched negatives, published into a
    /// roll rather than a bare output folder (Phase 3 section 3.5). This is
    /// the app's normal path — `Run` builds this, not `.convert`. `work` is
    /// left `nil` here: a chosen work directory is Chunk P2-10's re-stitch
    /// feature, not this one's. There is no `--overwrite`: replacing an
    /// existing negative is expressed by *not* skipping its sources
    /// (`skipSources`), which the overlap sheet derives (section 3.4).
    public static func run(
        input: URL,
        files: [String],
        roll: URL,
        across: Int? = nil,
        down: Int = 1,
        jobs: Int? = nil,
        skipSources: [String] = [],
        work: URL? = nil,
        flatfield: String? = nil
    ) -> CLICommand {
        var arguments = ["run", "--input", input.path]
        arguments.append("--files")
        arguments.append(contentsOf: files)
        arguments.append(contentsOf: ["--roll", roll.path])
        arguments.append(contentsOf: groupingArguments(across: across, down: down))
        if let jobs {
            arguments.append(contentsOf: ["--jobs", String(jobs)])
        }
        if let work {
            arguments.append(contentsOf: ["--work", work.path])
        }
        if let flatfield {
            arguments.append(contentsOf: ["--flatfield", flatfield])
        }
        if !skipSources.isEmpty {
            arguments.append("--skip-sources")
            arguments.append(contentsOf: skipSources)
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy apply-metadata --roll DIR`
    ///
    /// Section 3.8: writes every dirty negative's intended capture time into
    /// its published TIFF's EXIF tags. No pixel data is touched.
    public static func applyMetadata(roll: URL) -> CLICommand {
        CLICommand(arguments: ["apply-metadata", "--roll", roll.path])
    }

    /// `scanny-boy metadata set --roll DIR --payload JSON`
    ///
    /// Protocol version 9's extended-metadata editing: applies one payload
    /// of roll-level and/or per-negative metadata fields to the roll's
    /// record in the library database. It never touches a TIFF — metadata
    /// reaches those only at export. The whole payload is validated by the
    /// CLI before anything is written, and the `metadata_updated`
    /// confirmation carries the updated manifest.
    public static func metadataSet(roll: URL, payload: String) -> CLICommand {
        CLICommand(arguments: [
            "metadata", "set",
            "--roll", roll.path,
            "--payload", payload,
        ])
    }

    /// `scanny-boy metadata values --field FIELD`
    ///
    /// The catalog of previously-entered values for one canonical field
    /// (city, state, camera, lens), most-recently-used first — the list the
    /// Metadata tab's typeahead offers.
    public static func metadataValues(field: String) -> CLICommand {
        CLICommand(arguments: ["metadata", "values", "--field", field])
    }

    /// `scanny-boy edit rotate --roll DIR --negative ID [--negative ID ...] --direction cw|ccw`
    ///
    /// Protocol version 5's nondestructive edit: appends one rotation op per
    /// selected negative's ordered ops log in the library database,
    /// regenerates the CLI-rendered previews, and never touches the
    /// published TIFFs. The whole selection is validated by the CLI before
    /// anything is recorded.
    public static func editRotate(
        roll: URL, negatives: [String], clockwise: Bool
    ) -> CLICommand {
        var arguments = [
            "edit", "rotate",
            "--roll", roll.path,
        ]
        for negative in negatives {
            arguments.append(contentsOf: ["--negative", negative])
        }
        arguments.append(contentsOf: ["--direction", clockwise ? "cw" : "ccw"])
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy edit flip --roll DIR --negative ID [--negative ID ...]`
    ///
    /// Records a horizontal mirror of the pixels as they currently render —
    /// *after* any recorded rotations — per selected negative, appending a
    /// `flip` op to each one's ordered ops log. Never touches the published
    /// TIFFs.
    public static func editFlip(roll: URL, negatives: [String]) -> CLICommand {
        var arguments = [
            "edit", "flip",
            "--roll", roll.path,
        ]
        for negative in negatives {
            arguments.append(contentsOf: ["--negative", negative])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy edit delete --roll DIR --negative ID [--negative ID ...]`
    ///
    /// The one destructive edit: removes each selected negative's record
    /// (and its ops log, by cascade) from the library database, unlinks its
    /// published TIFF from the roll folder, and unlinks its rendered
    /// preview. The confirmation dialog lives in the view layer; by the
    /// time this command is built the user has already agreed.
    public static func editDelete(roll: URL, negatives: [String]) -> CLICommand {
        var arguments = [
            "edit", "delete",
            "--roll", roll.path,
        ]
        for negative in negatives {
            arguments.append(contentsOf: ["--negative", negative])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy edit render-region --roll DIR --negative ID --x PX --y PX --width PX --height PX --output PATH`
    ///
    /// Protocol version 9: renders one display-space region of the
    /// negative's published TIFF at 1:1 — the net rotation folded in, the
    /// same display encode as the cached preview — into `output` as a
    /// lossless PNG. A pure rendering query backing the Edit tab's 100%
    /// zoom: nothing is recorded, the TIFF is never touched.
    public static func editRenderRegion(
        roll: URL,
        negative: String,
        x: Int,
        y: Int,
        width: Int,
        height: Int,
        output: URL
    ) -> CLICommand {
        CLICommand(arguments: [
            "edit", "render-region",
            "--roll", roll.path,
            "--negative", negative,
            "--x", String(x),
            "--y", String(y),
            "--width", String(width),
            "--height", String(height),
            "--output", output.path,
        ])
    }

    /// `scanny-boy export --roll DIR --output DIR [--negatives ID ...]`
    ///
    /// Applies each negative's recorded edits to its published pixels and
    /// writes the result into the chosen folder; the roll's own TIFFs are
    /// never modified. No selection means every negative.
    public static func export(roll: URL, output: URL, negatives: [String] = []) -> CLICommand {
        var arguments = ["export", "--roll", roll.path, "--output", output.path]
        if !negatives.isEmpty {
            arguments.append("--negatives")
            arguments.append(contentsOf: negatives)
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy stitch --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial] [--flatfield ID]`
    ///
    /// Chunk P2-10's re-stitch path: reads the Phase 1 manifest already in
    /// `work`, verifies every intermediate, and stitches — without paying for
    /// RAW decoding again. `allowPartial` defaults to `true` because a kept
    /// work directory is exactly as likely to be `partial` (kept because one
    /// negative failed) as `complete`, and passing it is a no-op when the
    /// manifest is already `complete`. `overwrite` is only ever set after the
    /// user has explicitly agreed (section 3.6). `--out` became `--roll` in
    /// Phase 3 section 3.5; a re-stitch's target is a roll folder same as
    /// everything else now. `flatfield` names the calibration profile whose
    /// geometry reaches the stitch warp (protocol version 7); a roll locked
    /// to a profile's geometry refuses a stitch without it.
    public static func stitch(
        work: URL,
        roll: URL,
        jobs: Int? = nil,
        overwrite: Bool = false,
        allowPartial: Bool = true,
        flatfield: String? = nil
    ) -> CLICommand {
        var arguments = ["stitch", "--work", work.path, "--roll", roll.path]
        if let jobs {
            arguments.append(contentsOf: ["--jobs", String(jobs)])
        }
        if overwrite {
            arguments.append("--overwrite")
        }
        if allowPartial {
            arguments.append("--allow-partial")
        }
        if let flatfield {
            arguments.append(contentsOf: ["--flatfield", flatfield])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy flatfield create --reference FILE --name NAME [--calibration FILE ...]`
    ///
    /// Protocol version 7: decodes the bare light source reference, builds
    /// and stores the gain map, and inserts the profile. With
    /// `calibrationFrames` (ChArUco board NEFs, absolute paths), the profile
    /// additionally carries the geometric calibration — and the command runs
    /// for minutes, driven by `flatfield_progress` events.
    public static func flatfieldCreate(
        reference: URL,
        name: String,
        calibrationFrames: [URL] = []
    ) -> CLICommand {
        var arguments = [
            "flatfield", "create",
            "--reference", reference.path,
            "--name", name,
        ]
        if !calibrationFrames.isEmpty {
            arguments.append("--calibration")
            arguments.append(contentsOf: calibrationFrames.map(\.path))
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy flatfield list`
    public static func flatfieldList() -> CLICommand {
        CLICommand(arguments: ["flatfield", "list"])
    }

    /// `scanny-boy flatfield delete --profile ID`
    ///
    /// The CLI refuses with `FLATFIELD_PROFILE_IN_USE` when any roll's
    /// invariants name the profile; the app surfaces that as an alert.
    public static func flatfieldDelete(profile: String) -> CLICommand {
        CLICommand(arguments: ["flatfield", "delete", "--profile", profile])
    }
}

/// Starts CLI sessions against one resolved executable.
///
/// The executable is resolved once, when the runner is created, so a missing
/// helper is reported before any run is attempted rather than on each command.
public struct CLIRunner: Sendable {
    public let executable: URL
    /// Merged over this process's own environment for every session this
    /// runner starts; empty by default, which reproduces the old
    /// inherit-everything behaviour exactly. Tests that invoke the real
    /// bundled helper use this to point `SCANNY_BOY_LIBRARY_DB` at a
    /// per-test database, so they never touch the user's real library.
    public let environmentOverrides: [String: String]

    public init(executable: URL, environmentOverrides: [String: String] = [:]) {
        self.executable = executable
        self.environmentOverrides = environmentOverrides
    }

    public init(locator: CLILocator = .mainBundle()) throws {
        self.init(executable: try locator.locate())
    }

    public func session(for command: CLICommand) -> CLISession {
        var environment: [String: String]?
        if !environmentOverrides.isEmpty {
            environment = ProcessInfo.processInfo.environment
            environment!.merge(environmentOverrides) { _, override in override }
        }
        return CLISession(
            configuration: CLISession.Configuration(
                executable: executable,
                arguments: command.arguments,
                environment: environment
            )
        )
    }
}

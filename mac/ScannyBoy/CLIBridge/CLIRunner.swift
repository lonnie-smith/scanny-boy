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

    /// `scanny-boy probe --input DIR [--files ...] [--out DIR] [--roll DIR] [--per-negative N]`
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
        perNegative: Int? = nil,
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
        if let perNegative {
            arguments.append(contentsOf: ["--per-negative", String(perNegative)])
        }
        if let flatfield {
            arguments.append(contentsOf: ["--flatfield", flatfield])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy convert --input DIR --files ... --out DIR [--per-negative N] ...`
    ///
    /// There is no `--film-date`: Phase 3 removed it from every command, and
    /// the CLI derives `film_date` from the scans' own capture times
    /// (CONTRACT.md). `overwrite` is only ever set after the user has
    /// confirmed the replacements (section 3.6).
    public static func convert(
        input: URL,
        files: [String],
        out: URL,
        perNegative: Int? = nil,
        jobs: Int? = nil,
        overwrite: Bool = false,
        flatfield: String? = nil
    ) -> CLICommand {
        var arguments = ["convert", "--input", input.path]
        arguments.append("--files")
        arguments.append(contentsOf: files)
        arguments.append(contentsOf: ["--out", out.path])
        if let perNegative {
            arguments.append(contentsOf: ["--per-negative", String(perNegative)])
        }
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
        perNegative: Int? = nil,
        jobs: Int? = nil,
        skipSources: [String] = [],
        work: URL? = nil,
        flatfield: String? = nil
    ) -> CLICommand {
        var arguments = ["run", "--input", input.path]
        arguments.append("--files")
        arguments.append(contentsOf: files)
        arguments.append(contentsOf: ["--roll", roll.path])
        if let perNegative {
            arguments.append(contentsOf: ["--per-negative", String(perNegative)])
        }
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

    /// `scanny-boy edit rotate --roll DIR --negative ID --direction cw|ccw`
    ///
    /// Protocol version 5's nondestructive edit: appends one rotation op to
    /// the negative's ordered ops log in the library database, regenerates
    /// the CLI-rendered preview, and never touches the published TIFF.
    public static func editRotate(roll: URL, negative: String, clockwise: Bool) -> CLICommand {
        CLICommand(arguments: [
            "edit", "rotate",
            "--roll", roll.path,
            "--negative", negative,
            "--direction", clockwise ? "cw" : "ccw",
        ])
    }

    /// `scanny-boy edit delete --roll DIR --negative ID`
    ///
    /// The one destructive edit: removes the negative's record (and its
    /// ops log, by cascade) from the library database, unlinks its
    /// published TIFF from the roll folder, and unlinks its rendered
    /// preview. The confirmation dialog lives in the view layer; by the
    /// time this command is built the user has already agreed.
    public static func editDelete(roll: URL, negative: String) -> CLICommand {
        CLICommand(arguments: [
            "edit", "delete",
            "--roll", roll.path,
            "--negative", negative,
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

    /// `scanny-boy stitch --work DIR --roll DIR [--jobs N] [--overwrite] [--allow-partial]`
    ///
    /// Chunk P2-10's re-stitch path: reads the Phase 1 manifest already in
    /// `work`, verifies every intermediate, and stitches — without paying for
    /// RAW decoding again. `allowPartial` defaults to `true` because a kept
    /// work directory is exactly as likely to be `partial` (kept because one
    /// negative failed) as `complete`, and passing it is a no-op when the
    /// manifest is already `complete`. `overwrite` is only ever set after the
    /// user has explicitly agreed (section 3.6). `--out` became `--roll` in
    /// Phase 3 section 3.5; a re-stitch's target is a roll folder same as
    /// everything else now.
    public static func stitch(
        work: URL,
        roll: URL,
        jobs: Int? = nil,
        overwrite: Bool = false,
        allowPartial: Bool = true
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
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy flatfield create --reference FILE --name NAME`
    ///
    /// Protocol version 6: decodes the bare light source reference, builds
    /// and stores the gain map, and inserts the profile. Takes seconds — the
    /// UI shows a spinner, not a progress bar.
    public static func flatfieldCreate(reference: URL, name: String) -> CLICommand {
        CLICommand(arguments: [
            "flatfield", "create",
            "--reference", reference.path,
            "--name", name,
        ])
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

    public init(executable: URL) {
        self.executable = executable
    }

    public init(locator: CLILocator = .mainBundle()) throws {
        self.init(executable: try locator.locate())
    }

    public func session(for command: CLICommand) -> CLISession {
        CLISession(
            configuration: CLISession.Configuration(
                executable: executable,
                arguments: command.arguments
            )
        )
    }
}

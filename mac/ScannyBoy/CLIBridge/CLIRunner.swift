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

    /// `scanny-boy probe --input DIR [--files ...] [--out DIR] [--per-negative N]`
    ///
    /// With `--input` alone this returns the catalogue in canonical order.
    /// Adding `--files` also validates the selection; adding `--out` on top of
    /// that includes output-folder validation and the overwrite-conflict
    /// preview.
    public static func probe(
        input: URL,
        files: [String] = [],
        out: URL? = nil,
        perNegative: Int? = nil
    ) -> CLICommand {
        var arguments = ["probe", "--input", input.path]
        if !files.isEmpty {
            arguments.append("--files")
            arguments.append(contentsOf: files)
        }
        if let out {
            arguments.append(contentsOf: ["--out", out.path])
        }
        if let perNegative {
            arguments.append(contentsOf: ["--per-negative", String(perNegative)])
        }
        return CLICommand(arguments: arguments)
    }

    /// `scanny-boy convert --input DIR --files ... --out DIR --film-date YYYY-MM-DD ...`
    ///
    /// `filmDate` is passed through as written; the CLI validates its format
    /// and rejects a bad one as a usage error. `overwrite` is only ever set
    /// after the user has confirmed the replacements (section 3.6).
    public static func convert(
        input: URL,
        files: [String],
        out: URL,
        filmDate: String,
        perNegative: Int? = nil,
        jobs: Int? = nil,
        overwrite: Bool = false
    ) -> CLICommand {
        var arguments = ["convert", "--input", input.path]
        arguments.append("--files")
        arguments.append(contentsOf: files)
        arguments.append(contentsOf: ["--out", out.path])
        arguments.append(contentsOf: ["--film-date", filmDate])
        if let perNegative {
            arguments.append(contentsOf: ["--per-negative", String(perNegative)])
        }
        if let jobs {
            arguments.append(contentsOf: ["--jobs", String(jobs)])
        }
        if overwrite {
            arguments.append("--overwrite")
        }
        return CLICommand(arguments: arguments)
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

import Foundation

/// Invokes the bundled `scanny-boy` CLI binary and decodes its JSON output.
/// See shared/contract/CONTRACT.md for the interface this wraps.
enum CLIRunner {
    enum CLIError: Error {
        case binaryNotFound
        case nonZeroExit(status: Int32, stderr: String)
        case decodingFailed(Error)
    }

    private static var binaryURL: URL? {
        Bundle.main.url(forResource: "scanny-boy", withExtension: nil, subdirectory: "cli")
    }

    static func scan(path: String) throws -> ScanResult {
        guard let binaryURL else { throw CLIError.binaryNotFound }

        let process = Process()
        process.executableURL = binaryURL
        process.arguments = ["scan", path]

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        try process.run()
        process.waitUntilExit()

        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()

        guard process.terminationStatus == 0 else {
            let errString = String(data: errData, encoding: .utf8) ?? ""
            throw CLIError.nonZeroExit(status: process.terminationStatus, stderr: errString)
        }

        do {
            return try JSONDecoder().decode(ScanResult.self, from: outData)
        } catch {
            throw CLIError.decodingFailed(error)
        }
    }
}

import Darwin
import Foundation

/// The only place a tethered capture file name is chosen
/// (docs/TETHER_PLAN.md §2.6).
enum CaptureNaming {
    private static let stampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter
    }()

    /// Returns `<yyyyMMdd-HHmmss>_<cc>.NEF` for the negative's first release
    /// time and the shot number within the negative (`01` upward).
    static func filename(firstRelease: Date, shotNumber: Int) -> String {
        let stamp = stampFormatter.string(from: firstRelease)
        let cell = String(format: "%02d", shotNumber)
        return "\(stamp)_\(cell).NEF"
    }

    /// Creates a unique file URL in `directory`, using `O_EXCL`. When the
    /// base name already exists, appends `-2`, `-3`, … to the timestamp
    /// portion before the `_cc` suffix.
    static func exclusiveURL(
        in directory: URL,
        firstRelease: Date,
        shotNumber: Int
    ) throws -> URL {
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
        var suffix = 0
        while true {
            let stamp = stampFormatter.string(from: firstRelease)
            let adjustedStamp = suffix == 0 ? stamp : "\(stamp)-\(suffix + 1)"
            let cell = String(format: "%02d", shotNumber)
            let name = "\(adjustedStamp)_\(cell).NEF"
            let url = directory.appending(path: name, directoryHint: .notDirectory)
            let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL, 0o644)
            if fd >= 0 {
                close(fd)
                try? FileManager.default.removeItem(at: url)
                return url
            }
            if errno != EEXIST {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            suffix += 1
        }
    }
}

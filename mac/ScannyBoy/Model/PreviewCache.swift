import CryptoKit
import Foundation

/// The app's own render caches: the 1:1 region crops behind the Edit tab's
/// 100% zoom, and the whole-image renders behind the positive/negative
/// toggle. Both hold PNGs the CLI wrote for one negative in one cache
/// generation (`EditModel.renderGeneration`), and both are pure caches — a
/// missing file costs one `edit render-region`/`render-preview` round trip
/// and nothing else.
///
/// Layout is `<caches>/ScannyBoy/<kind>/<roll id>/<negative>-g<generation>…`.
/// The roll level is what makes the caches collectable: the file names key
/// on the negative alone, so without it a deleted roll's crops could never
/// be named again — and a roll is deleted exactly when its whole cache
/// becomes unreachable. `RollLibrary.deleteRoll` removes that directory.
///
/// The app is not sandboxed (`project.yml`: "Phase 1 is explicitly not
/// sandboxed"), so `URL.cachesDirectory` is the user's own
/// `~/Library/Caches`; everything this type writes therefore lives under one
/// `ScannyBoy` directory rather than loose beside other applications' caches.
struct PreviewCache: Sendable {
    /// The system caches directory the store lives in — injected in tests so
    /// a test never reads or removes the real user's caches.
    let cachesDirectory: URL

    static let shared = PreviewCache(cachesDirectory: .cachesDirectory)

    /// Everything this app caches, under one directory.
    var root: URL {
        cachesDirectory.appending(path: "ScannyBoy", directoryHint: .isDirectory)
    }

    private var regionsRoot: URL {
        root.appending(path: "preview-regions", directoryHint: .isDirectory)
    }

    private var renderedPreviewsRoot: URL {
        root.appending(path: "rendered-previews", directoryHint: .isDirectory)
    }

    /// Where `edit render-region` should write one 1:1 crop. The rect is part
    /// of the name because each scroll position is its own render; the
    /// generation is, because an edit changes the pixels under every rect.
    func regionURL(
        rollID: String,
        negativeID: String,
        generation: String,
        mode: PreviewDisplayMode,
        rect: CGRect
    ) -> URL {
        let name = """
            \(negativeID)-g\(Self.generationComponent(generation))-\(mode.rawValue)\
            -\(Int(rect.minX))-\(Int(rect.minY))-\(Int(rect.width))-\(Int(rect.height)).png
            """
        return directory(regionsRoot, forRoll: rollID)
            .appending(path: name, directoryHint: .notDirectory)
    }

    /// Where `edit render-preview` should write one whole-image render.
    func previewURL(
        rollID: String,
        negativeID: String,
        generation: String,
        mode: PreviewDisplayMode
    ) -> URL {
        let name =
            "\(negativeID)-g\(Self.generationComponent(generation))-\(mode.rawValue).png"
        return directory(renderedPreviewsRoot, forRoll: rollID)
            .appending(path: name, directoryHint: .notDirectory)
    }

    /// Drop everything cached for one roll, both kinds. Called after
    /// `roll delete` succeeds: the roll's negatives can never be rendered
    /// again, so nothing here can ever be hit.
    ///
    /// Best effort by design, exactly like the CLI's own
    /// `ORPHAN_FILE_NOT_REMOVED` warning: the roll is already gone, so a
    /// failure here must not turn a completed deletion into an error the
    /// user sees. A missing directory is the ordinary case (a roll that was
    /// never opened in the Edit tab caches nothing).
    func removeAll(forRoll rollID: String) {
        guard Self.isSafePathComponent(rollID) else { return }
        for kind in [regionsRoot, renderedPreviewsRoot] {
            try? FileManager.default.removeItem(
                at: kind.appending(path: rollID, directoryHint: .isDirectory)
            )
        }
    }

    /// Remove the caches written before the layout above existed: two
    /// directories loose in `~/Library/Caches`, holding files named by
    /// negative with no roll anywhere in the path. Nothing can match those
    /// against a live roll — that is the bug this layout fixes — so the only
    /// correct treatment of the whole set is to drop it. They are caches:
    /// what is still wanted is re-rendered on demand.
    ///
    /// Called once at launch. Safe to call when the directories are already
    /// gone, which is every launch after the first.
    func purgeUnscopedCaches() {
        for name in ["preview-regions", "rendered-previews"] {
            try? FileManager.default.removeItem(
                at: cachesDirectory.appending(path: name, directoryHint: .isDirectory)
            )
        }
    }

    private func directory(_ kind: URL, forRoll rollID: String) -> URL {
        kind.appending(path: rollID, directoryHint: .isDirectory)
    }

    /// A short, filename-safe stand-in for `generation`, which folds in
    /// unbounded CLI-reported text — `EditModel.renderGeneration`'s camera
    /// colour term carries the full RGB→XYZ matrix and camera model name
    /// verbatim, so a long model name (e.g. "NIKON CORPORATION NIKON Z f")
    /// pushed a region filename past macOS's 255-byte component limit and
    /// `edit render-region` failed every request with `INTERNAL_ERROR`
    /// (`OSError: File name too long`) — silently, from the zoom UI's
    /// perspective, since nothing there distinguishes a failed fetch from
    /// one still in flight: the 100% zoom just spun forever. Hashing keeps
    /// the name short regardless of how large `generation` grows, and still
    /// changes whenever `generation` does, which is all a cache key needs.
    private static func generationComponent(_ generation: String) -> String {
        let digest = SHA256.hash(data: Data(generation.utf8))
        return digest.map { String(format: "%02x", $0) }.prefix(16).joined()
    }

    /// A roll id is a UUID (`roll_folder.create_roll`), but it reaches the
    /// app as a string off the event stream, and it is about to become a
    /// path component that something recursively deletes. Anything that
    /// could escape the cache directory is refused rather than sanitised.
    private static func isSafePathComponent(_ value: String) -> Bool {
        !value.isEmpty && value != "." && value != ".." && !value.contains("/")
    }
}

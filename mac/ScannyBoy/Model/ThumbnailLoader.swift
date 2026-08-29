import AppKit
import CoreGraphics
import ImageIO
import QuickLookThumbnailing

/// One finished thumbnail.
///
/// `NSImage` is not `Sendable`, and a thumbnail is produced away from the
/// main actor and consumed on it. This box carries it across that boundary:
/// the image is built from an immutable `CGImage`, is never drawn into or
/// otherwise mutated after it is made, and is handed to exactly one owner at
/// a time.
struct Thumbnail: @unchecked Sendable {
    let image: NSImage
}

/// Renders and caches small previews of the catalogue's RAW files so the
/// selection list can show pictures rather than filenames alone.
///
/// This is display only. It reads nothing the CLI has not already reported —
/// every URL handed to it is built from a `probe` catalogue entry — and it
/// never influences the catalogue, the selection, or anything sent back to
/// the CLI, so section 3.2's rule that Python owns all logic is untouched.
///
/// Two native paths produce the picture, in order:
///
/// 1. QuickLook (`QLThumbnailGenerator`) — the same machinery the Finder
///    uses, so a NEF the Finder can show, this can show. It keeps a
///    system-wide on-disk cache, which is what makes revisiting a folder
///    cheap.
/// 2. ImageIO (`CGImageSourceCreateThumbnailAtIndex`) — reads the JPEG
///    preview Nikon embeds in the NEF itself. This is the fallback for when
///    QuickLook declines: no thumbnail extension available, or its daemon
///    unreachable.
///
/// Neither path demosaics the RAW frame, which is why a folder of
/// 40-megapixel negatives still fills in quickly.
actor ThumbnailLoader {
    /// The app's one loader. Sharing it is the point: the cache and the
    /// concurrency ceiling only mean anything if every row goes through the
    /// same instance.
    static let shared = ThumbnailLoader()

    /// How many thumbnails may be generated at once.
    ///
    /// A `List` only builds the rows it is showing, but scrolling quickly
    /// through a long catalogue starts a request for every row it passes.
    /// Without a ceiling those all run at once and compete; with one they
    /// queue, and the rows still on screen finish first.
    private static let defaultConcurrentGenerations = 4

    private let cache = NSCache<NSString, CachedThumbnail>()
    private var inFlight: [String: Task<Thumbnail?, Never>] = [:]
    private let throttle: ThumbnailThrottle

    init(cacheLimit: Int = 512, concurrentGenerations: Int? = nil) {
        cache.countLimit = cacheLimit
        throttle = ThumbnailThrottle(
            limit: concurrentGenerations ?? Self.defaultConcurrentGenerations
        )
    }

    /// The thumbnail for `url` at `pointSize`, or `nil` if neither path could
    /// produce one.
    ///
    /// Callers asking for the same file at the same size share one
    /// generation rather than starting several, and a result — including
    /// "there isn't one" — is produced once and then served from the cache,
    /// so a file without a preview is not retried on every scroll.
    func thumbnail(for url: URL, pointSize: CGSize, scale: CGFloat) async -> Thumbnail? {
        let key = Self.cacheKey(url: url, pointSize: pointSize, scale: scale) as NSString
        if let cached = cache.object(forKey: key) {
            return cached.thumbnail
        }
        if let existing = inFlight[key as String] {
            return await existing.value
        }

        let throttle = throttle
        let task = Task.detached(priority: .userInitiated) { () -> Thumbnail? in
            await throttle.acquire()
            defer { throttle.release() }
            return await Self.generate(url: url, pointSize: pointSize, scale: scale)
        }
        inFlight[key as String] = task
        let thumbnail = await task.value
        inFlight[key as String] = nil
        cache.setObject(CachedThumbnail(thumbnail: thumbnail), forKey: key)
        return thumbnail
    }

    /// Forgets every cached thumbnail. The app does not need this — cache
    /// keys include the file's path, so a different folder simply misses —
    /// but a test that wants a cold loader does.
    func removeAll() {
        cache.removeAllObjects()
        inFlight.removeAll()
    }

    /// `NSCache` stores objects and cannot store "nothing", which is exactly
    /// the answer worth remembering for a file that has no preview.
    private final class CachedThumbnail {
        let thumbnail: Thumbnail?

        init(thumbnail: Thumbnail?) {
            self.thumbnail = thumbnail
        }
    }

    private static func cacheKey(url: URL, pointSize: CGSize, scale: CGFloat) -> String {
        "\(url.path)|\(Int(pointSize.width))x\(Int(pointSize.height))@\(scale)"
    }

    // MARK: - The two generation paths

    /// QuickLook first, then the RAW file's own embedded preview.
    static func generate(url: URL, pointSize: CGSize, scale: CGFloat) async -> Thumbnail? {
        if let thumbnail = await quickLookThumbnail(url: url, pointSize: pointSize, scale: scale) {
            return thumbnail
        }
        return embeddedPreview(url: url, pointSize: pointSize, scale: scale)
    }

    /// The Finder's own thumbnail for this file.
    ///
    /// `.thumbnail` asks for a picture *of the file* and nothing else: the
    /// other representation types will happily return a generic document
    /// icon, which is a worse row than the filename on its own.
    static func quickLookThumbnail(
        url: URL, pointSize: CGSize, scale: CGFloat
    ) async -> Thumbnail? {
        let request = QLThumbnailGenerator.Request(
            fileAt: url,
            size: pointSize,
            scale: scale,
            representationTypes: .thumbnail
        )
        guard
            let representation = try? await QLThumbnailGenerator.shared
                .generateBestRepresentation(for: request)
        else {
            return nil
        }
        return Thumbnail(image: representation.nsImage)
    }

    /// The JPEG preview embedded in the RAW file, scaled down by ImageIO.
    ///
    /// `kCGImageSourceCreateThumbnailWithTransform` applies the camera's
    /// orientation, so a portrait frame shows upright — the same rule
    /// section 3.4 sets for the converted TIFFs, applied here only to the
    /// picture on screen.
    static func embeddedPreview(url: URL, pointSize: CGSize, scale: CGFloat) -> Thumbnail? {
        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let source = CGImageSourceCreateWithURL(url as CFURL, sourceOptions) else {
            return nil
        }
        let maximumPixelSize = Int((max(pointSize.width, pointSize.height) * scale).rounded(.up))
        let options =
            [
                kCGImageSourceCreateThumbnailFromImageIfAbsent: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: maximumPixelSize,
            ] as CFDictionary
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else {
            return nil
        }
        return Thumbnail(
            image: NSImage(
                cgImage: image,
                size: NSSize(
                    width: CGFloat(image.width) / scale,
                    height: CGFloat(image.height) / scale
                )
            )
        )
    }
}

/// An async counting semaphore: at most `limit` holders at a time, everyone
/// else waits their turn in arrival order.
actor ThumbnailThrottle {
    private let limit: Int
    private var active = 0
    private var waiting: [CheckedContinuation<Void, Never>] = []

    init(limit: Int) {
        self.limit = max(1, limit)
    }

    func acquire() async {
        guard active >= limit else {
            active += 1
            return
        }
        await withCheckedContinuation { continuation in
            waiting.append(continuation)
        }
    }

    /// `nonisolated` so a `defer` can call it without awaiting: the release
    /// is handed to the actor and the releasing task carries on.
    nonisolated func release() {
        Task { await self.finish() }
    }

    private func finish() {
        if waiting.isEmpty {
            active -= 1
        } else {
            // The slot passes straight to the next waiter, so `active` stays
            // where it is.
            waiting.removeFirst().resume()
        }
    }

    /// Test-only view of how many holders there are right now.
    var activeCount: Int { active }
}

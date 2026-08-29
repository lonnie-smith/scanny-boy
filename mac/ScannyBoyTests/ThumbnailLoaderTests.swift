import AppKit
import Foundation
import Testing

@testable import ScannyBoy

/// `ThumbnailLoader` and the throttle it queues work through.
///
/// The cases that need a real NEF need a *real* one: what is being checked is
/// that macOS can turn a Nikon Z f RAW file into a picture at all, which no
/// synthetic fixture can stand in for. They skip, loudly, when the sample
/// files are absent (section 7).
struct ThumbnailLoaderTests {
    private static let size = CGSize(width: 80, height: 80)

    /// Why a case that needs a real RAW file was skipped, naming what went
    /// untested.
    static let samplesUnavailable: Comment = """
        The real sample NEFs are not present at tests/fixtures/nef/ (see \
        docs/IMPLEMENTATION_PLAN.md appendix A). Rendering a catalogue \
        thumbnail from a real Nikon RAW file — through QuickLook and through \
        the NEF's own embedded preview — did not run.
        """

    private static var sampleNEF: URL {
        SampleFixtures.directory.appending(path: SampleFixtures.files[0])
    }

    @Test(
        .enabled(if: SampleFixtures.areAvailable, ThumbnailLoaderTests.samplesUnavailable)
    )
    func rendersAThumbnailForARealNEF() async throws {
        let loader = ThumbnailLoader()
        let thumbnail = try #require(
            await loader.thumbnail(for: Self.sampleNEF, pointSize: Self.size, scale: 2)
        )

        // A picture of the frame, not a placeholder: some pixels, and no
        // larger than what was asked for.
        #expect(thumbnail.image.size.width > 0)
        #expect(thumbnail.image.size.height > 0)
        #expect(max(thumbnail.image.size.width, thumbnail.image.size.height) <= 80.5)
    }

    /// The fallback path on its own. QuickLook is the preferred route, but it
    /// depends on a system daemon and on whichever thumbnail extensions are
    /// installed; this proves the app can still show the frame without it.
    @Test(
        .enabled(if: SampleFixtures.areAvailable, ThumbnailLoaderTests.samplesUnavailable)
    )
    func readsTheEmbeddedPreviewWithoutQuickLook() async throws {
        let thumbnail = try #require(
            ThumbnailLoader.embeddedPreview(url: Self.sampleNEF, pointSize: Self.size, scale: 2)
        )

        #expect(thumbnail.image.size.width > 0)
        #expect(thumbnail.image.size.height > 0)
    }

    /// A NEF is landscape or portrait, never square: whichever path produced
    /// the picture, it must not have been squashed into the requested box.
    @Test(
        .enabled(if: SampleFixtures.areAvailable, ThumbnailLoaderTests.samplesUnavailable)
    )
    func keepsTheFrameProportions() async throws {
        let loader = ThumbnailLoader()
        let thumbnail = try #require(
            await loader.thumbnail(for: Self.sampleNEF, pointSize: Self.size, scale: 2)
        )

        #expect(thumbnail.image.size.width != thumbnail.image.size.height)
    }

    @Test(
        .enabled(if: SampleFixtures.areAvailable, ThumbnailLoaderTests.samplesUnavailable)
    )
    func servesRepeatedRequestsFromItsCache() async throws {
        let loader = ThumbnailLoader()
        let first = try #require(
            await loader.thumbnail(for: Self.sampleNEF, pointSize: Self.size, scale: 2)
        )
        let second = try #require(
            await loader.thumbnail(for: Self.sampleNEF, pointSize: Self.size, scale: 2)
        )

        #expect(first.image === second.image)
    }

    /// Concurrent callers for the same file share one generation, so the
    /// second and later ones get the very same image the first produced.
    @Test(
        .enabled(if: SampleFixtures.areAvailable, ThumbnailLoaderTests.samplesUnavailable)
    )
    func coalescesConcurrentRequestsForTheSameFile() async throws {
        let loader = ThumbnailLoader()
        let url = Self.sampleNEF
        let thumbnails = await withTaskGroup(of: Thumbnail?.self) { group in
            for _ in 0..<8 {
                group.addTask {
                    await loader.thumbnail(for: url, pointSize: Self.size, scale: 2)
                }
            }
            return await group.reduce(into: [Thumbnail?]()) { $0.append($1) }
        }

        #expect(thumbnails.count == 8)
        let first = try #require(thumbnails.first.flatMap { $0 }?.image)
        #expect(thumbnails.allSatisfy { $0?.image === first })
    }

    /// A `.NEF` neither path can read — a truncated file, or one of the
    /// High Efficiency captures this project cannot decode at all (section 3,
    /// "Input rules") — yields no thumbnail rather than a generic document
    /// icon. The row then says "no preview", which is more honest than a
    /// picture of a page.
    @Test func reportsNoThumbnailForAnUnreadableNEF() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let url = directory.appending(path: "broken.NEF", directoryHint: .notDirectory)
            try Data("not a RAW file".utf8).write(to: url)

            let loader = ThumbnailLoader()
            #expect(await loader.thumbnail(for: url, pointSize: Self.size, scale: 2) == nil)
        }
    }

    @Test func reportsNoThumbnailForAMissingFile() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let url = directory.appending(path: "gone.NEF", directoryHint: .notDirectory)

            let loader = ThumbnailLoader()
            #expect(await loader.thumbnail(for: url, pointSize: Self.size, scale: 2) == nil)
        }
    }
}

struct ThumbnailThrottleTests {
    /// Scrolling a long catalogue asks for far more thumbnails than should be
    /// generated at once; the throttle is what turns that into a queue.
    @Test func neverExceedsItsLimit() async {
        let throttle = ThumbnailThrottle(limit: 3)
        let observed = Observed()

        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<20 {
                group.addTask {
                    await throttle.acquire()
                    await observed.record(await throttle.activeCount)
                    try? await Task.sleep(for: .milliseconds(2))
                    throttle.release()
                }
            }
        }

        #expect(await observed.peak <= 3)
        #expect(await observed.peak > 0)
    }

    /// Every waiter is eventually let through: 20 tasks through 2 slots all
    /// finish, which is only true if `release` hands the slot on.
    @Test func letsEveryWaiterThrough() async {
        let throttle = ThumbnailThrottle(limit: 2)
        let observed = Observed()

        await withTaskGroup(of: Void.self) { group in
            for _ in 0..<20 {
                group.addTask {
                    await throttle.acquire()
                    await observed.record(await throttle.activeCount)
                    throttle.release()
                }
            }
        }

        #expect(await observed.count == 20)
    }

    private actor Observed {
        private(set) var peak = 0
        private(set) var count = 0

        func record(_ active: Int) {
            peak = max(peak, active)
            count += 1
        }
    }
}

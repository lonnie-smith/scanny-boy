import Foundation
import Testing

@testable import ScannyBoy

/// The render caches' layout and its one job: being collectable. Every test
/// injects `cachesDirectory` — nothing here may read or remove anything in
/// the real `~/Library/Caches`.
@Suite("Preview cache")
struct PreviewCacheTests {
    private static func makeTemporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appending(path: "scanny-boy-tests", directoryHint: .isDirectory)
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func write(_ url: URL) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try Data("png".utf8).write(to: url)
    }

    @Test("Both kinds of render put the roll id in the path")
    func testPathsAreScopedByRoll() throws {
        let cache = PreviewCache(cachesDirectory: URL(filePath: "/caches"))

        let region = cache.regionURL(
            rollID: "roll-1", negativeID: "abc-negative-01", generation: "1#true#flat",
            mode: .positive, rect: CGRect(x: 10, y: 20, width: 30, height: 40)
        )
        let preview = cache.previewURL(
            rollID: "roll-1", negativeID: "abc-negative-01", generation: "1#true#flat",
            mode: .negative
        )

        #expect(region.path.hasPrefix("/caches/ScannyBoy/preview-regions/roll-1/abc-negative-01-g"))
        #expect(region.path.hasSuffix("-positive-10-20-30-40.rgba"))
        #expect(preview.path.hasPrefix("/caches/ScannyBoy/rendered-previews/roll-1/abc-negative-01-g"))
        #expect(preview.path.hasSuffix("-negative.png"))
    }

    @Test("The generation's separators never become path components")
    func testGenerationIsFlattenedIntoOneComponent() throws {
        let cache = PreviewCache(cachesDirectory: URL(filePath: "/caches"))

        let url = cache.previewURL(
            rollID: "roll-1", negativeID: "abc-negative-01",
            generation: "0#false#flat#neutral#none", mode: .positive
        )

        #expect(!url.lastPathComponent.contains("#"))
        #expect(url.deletingLastPathComponent().lastPathComponent == "roll-1")
    }

    @Test("The same generation always hashes to the same component, and different generations differ")
    func testGenerationHashIsStableAndDistinct() throws {
        let cache = PreviewCache(cachesDirectory: URL(filePath: "/caches"))

        let first = cache.previewURL(
            rollID: "roll-1", negativeID: "abc-negative-01", generation: "0#false#flat",
            mode: .positive
        )
        let repeated = cache.previewURL(
            rollID: "roll-1", negativeID: "abc-negative-01", generation: "0#false#flat",
            mode: .positive
        )
        let different = cache.previewURL(
            rollID: "roll-1", negativeID: "abc-negative-01", generation: "1#false#flat",
            mode: .positive
        )

        #expect(first.path == repeated.path)
        #expect(first.path != different.path)
    }

    /// Regression: `EditModel.renderGeneration`'s camera colour term carries
    /// the full RGB→XYZ matrix and camera model name verbatim — for a camera
    /// like "NIKON CORPORATION NIKON Z f" that alone is well over 100
    /// characters — and a region filename used to embed `generation`
    /// character-for-character. That pushed the filename past macOS's
    /// 255-byte component limit, and `edit render-region` failed every
    /// request with `INTERNAL_ERROR` ("File name too long"): the 100% zoom's
    /// crop never arrived, and nothing distinguished that failure from
    /// "still loading", so it just spun forever.
    @Test("An arbitrarily long generation still produces a short filename")
    func testLongGenerationProducesAShortFilename() throws {
        let cache = PreviewCache(cachesDirectory: URL(filePath: "/caches"))
        let longGeneration = "0#false#flat#neutral#none#false#"
            + "NIKON CORPORATION NIKON Z f#"
            + Array(repeating: "1.1606999635696411", count: 9).joined(separator: ",")

        let region = cache.regionURL(
            rollID: "roll-1", negativeID: "abc-negative-01", generation: longGeneration,
            mode: .positive, rect: CGRect(x: 4642, y: 5499, width: 852, height: 707)
        )

        #expect(region.lastPathComponent.utf8.count < 255)
    }

    @Test("removeAll takes both kinds for one roll and leaves the rest")
    func testRemoveAllIsScopedToOneRoll() throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = PreviewCache(cachesDirectory: directory)

        let doomedRegion = cache.regionURL(
            rollID: "roll-1", negativeID: "a-negative-01", generation: "g",
            mode: .positive, rect: CGRect(x: 0, y: 0, width: 1, height: 1)
        )
        let doomedPreview = cache.previewURL(
            rollID: "roll-1", negativeID: "a-negative-01", generation: "g", mode: .positive
        )
        let survivor = cache.previewURL(
            rollID: "roll-2", negativeID: "b-negative-01", generation: "g", mode: .positive
        )
        for url in [doomedRegion, doomedPreview, survivor] { try Self.write(url) }

        cache.removeAll(forRoll: "roll-1")

        #expect(!FileManager.default.fileExists(atPath: doomedRegion.path))
        #expect(!FileManager.default.fileExists(atPath: doomedPreview.path))
        #expect(FileManager.default.fileExists(atPath: survivor.path))
    }

    @Test("removeAll is quiet when the roll cached nothing")
    func testRemoveAllToleratesAMissingDirectory() throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        // A roll never opened in the Edit tab: the ordinary case, not a
        // failure — nothing to remove and nothing to report.
        PreviewCache(cachesDirectory: directory).removeAll(forRoll: "never-cached")
    }

    @Test("removeAll refuses a roll id that would escape the cache directory")
    func testRemoveAllRefusesTraversal() throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = PreviewCache(cachesDirectory: directory)
        let bystander = directory.appending(path: "bystander", directoryHint: .notDirectory)
        try Self.write(bystander)

        // The id arrives as a string off the event stream and is about to
        // become a path component something deletes recursively.
        cache.removeAll(forRoll: "..")
        cache.removeAll(forRoll: "../..")
        cache.removeAll(forRoll: "")

        #expect(FileManager.default.fileExists(atPath: bystander.path))
        #expect(FileManager.default.fileExists(atPath: directory.path))
    }

    @Test("purgeUnscopedCaches drops the pre-scoping caches and nothing else")
    func testPurgeRemovesTheOldFlatLayout() throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = PreviewCache(cachesDirectory: directory)

        // The old layout: loose in the caches directory, named by negative
        // with no roll anywhere in the path.
        let legacy = [
            directory.appending(path: "preview-regions/n-negative-01-g0-positive-0-0-8-8.png"),
            directory.appending(path: "rendered-previews/n-negative-01-g0-positive.png"),
        ]
        let current = cache.previewURL(
            rollID: "roll-1", negativeID: "n-negative-01", generation: "g", mode: .positive
        )
        let unrelated = directory.appending(path: "SomeOtherApp/cache.bin")
        for url in legacy + [current, unrelated] { try Self.write(url) }

        cache.purgeUnscopedCaches()

        for url in legacy {
            #expect(!FileManager.default.fileExists(atPath: url.path))
        }
        #expect(FileManager.default.fileExists(atPath: current.path))
        #expect(FileManager.default.fileExists(atPath: unrelated.path))
    }

    @Test("purgeUnscopedCaches is quiet on a launch with nothing to purge")
    func testPurgeToleratesMissingDirectories() throws {
        let directory = try Self.makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        // Every launch after the first.
        PreviewCache(cachesDirectory: directory).purgeUnscopedCaches()
    }
}

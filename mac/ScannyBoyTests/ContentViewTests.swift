import Foundation
import Testing

@testable import ScannyBoy

/// `ContentView.closestExistingAncestor(of:)`: the fallback that lets the
/// folder-choosing panel open somewhere sensible when the last-used folder
/// (persisted in `UserDefaults`, section 3.2) has since been renamed,
/// deleted, or lives on an unmounted volume.
struct ContentViewTests {
    @Test
    func returnsNilForNilInput() {
        #expect(ContentView.closestExistingAncestor(of: nil) == nil)
    }

    @Test
    func returnsTheURLItselfWhenItStillExists() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            #expect(ContentView.closestExistingAncestor(of: directory) == directory)
        }
    }

    @Test
    func walksUpToTheNearestExistingAncestor() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let missing = directory
                .appending(path: "renamed-roll", directoryHint: .isDirectory)
                .appending(path: "2026-01-01", directoryHint: .isDirectory)
            #expect(ContentView.closestExistingAncestor(of: missing) == directory)
        }
    }

    @Test
    func treatsAFileAtThePathAsNotADirectory() async throws {
        try await TestSupport.withTemporaryDirectory { directory in
            let file = directory.appending(path: "not-a-folder", directoryHint: .notDirectory)
            try Data().write(to: file)
            #expect(ContentView.closestExistingAncestor(of: file) == directory)
        }
    }
}

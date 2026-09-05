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

    // MARK: - shouldConfirmConvert

    @Test
    func shouldConfirmConvertReturnsFalseForNilRoll() {
        #expect(ContentView.shouldConfirmConvert(into: nil) == false)
    }

    @Test
    func shouldConfirmConvertReturnsFalseForEmptyRoll() {
        let roll = Roll(
            path: URL(filePath: "/tmp/empty"),
            status: .ok,
            reason: nil,
            rollID: "id-1",
            rollName: "Empty",
            negativeCount: 0
        )
        #expect(ContentView.shouldConfirmConvert(into: roll) == false)
    }

    @Test
    func shouldConfirmConvertReturnsFalseForUnreadableRoll() {
        let roll = Roll(
            path: URL(filePath: "/tmp/broken"),
            status: .unreadable,
            reason: Roll.Reason(code: .unknown(""), message: "unreadable"),
            rollID: nil,
            rollName: nil,
            negativeCount: 3
        )
        #expect(ContentView.shouldConfirmConvert(into: roll) == false)
    }

    @Test
    func shouldConfirmConvertReturnsTrueWhenRollHasNegatives() {
        let roll = Roll(
            path: URL(filePath: "/tmp/populated"),
            status: .ok,
            reason: nil,
            rollID: "id-2",
            rollName: "Tri-X",
            negativeCount: 2
        )
        #expect(ContentView.shouldConfirmConvert(into: roll) == true)
    }

    // MARK: - NewRollSheet.defaultName

    @Test
    func defaultRollNameIncludesDateAndTimeInUSLocale() {
        var components = DateComponents()
        components.year = 2026
        components.month = 8
        components.day = 12
        components.hour = 15
        components.minute = 34
        let calendar = Calendar(identifier: .gregorian)
        let date = calendar.date(from: components)!

        let name = NewRollSheet.defaultName(at: date, locale: Locale(identifier: "en_US"))

        #expect(name.contains("Aug"))
        #expect(name.contains("12"))
        #expect(name.contains("2026"))
        #expect(name.contains("3:34") || name.contains("15:34"))
    }

    @Test
    func defaultRollNameUsesLocaleFormatting() {
        var components = DateComponents()
        components.year = 2026
        components.month = 8
        components.day = 12
        components.hour = 15
        components.minute = 34
        let calendar = Calendar(identifier: .gregorian)
        let date = calendar.date(from: components)!

        let usName = NewRollSheet.defaultName(at: date, locale: Locale(identifier: "en_US"))
        let deName = NewRollSheet.defaultName(at: date, locale: Locale(identifier: "de_DE"))

        #expect(!usName.isEmpty)
        #expect(!deName.isEmpty)
        #expect(usName != deName)
    }
}

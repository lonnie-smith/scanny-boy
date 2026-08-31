import Foundation
import Testing

@testable import ScannyBoy

/// `OverlapReview`: the overlap sheet's per-row Skip/Replace decisions and
/// what they mean for `--skip-sources` (section 3.4/3.5). Kept independent
/// of any live `OverlapSheet` view or CLI call — these are plain data rules.
@Suite("Overlap review (Chunk P3-11)")
struct OverlapSheetTests {
    private static func entry(
        negativeID: String = "abc123-negative-01",
        expectedOutput: String = "a.tif",
        overlappingSources: [String] = ["a.NEF", "b.NEF", "c.NEF"],
        groupIndex: Int = 0
    ) -> RollOverlapEntry {
        RollOverlapEntry(
            negativeID: negativeID,
            expectedOutput: expectedOutput,
            runID: "abc123",
            overlappingSources: overlappingSources,
            groupIndex: groupIndex
        )
    }

    @Test("Every entry defaults to Skip")
    func testOverlapSheetDefaultsToSkip() {
        let entries = [
            Self.entry(negativeID: "n1", groupIndex: 0),
            Self.entry(negativeID: "n2", groupIndex: 1),
        ]
        let review = OverlapReview(entries: entries)
        for entry in entries {
            #expect(review.decision(for: entry) == .skip)
        }
    }

    @Test("Skip decisions become the --skip-sources arguments")
    func testSkipDecisionsBecomeSkipSourcesArguments() {
        let one = Self.entry(
            negativeID: "n1", overlappingSources: ["a.NEF", "b.NEF", "c.NEF"], groupIndex: 0
        )
        let two = Self.entry(
            negativeID: "n2", overlappingSources: ["d.NEF", "e.NEF", "f.NEF"], groupIndex: 1
        )
        let review = OverlapReview(entries: [one, two])
        #expect(Set(review.skipSources) == Set(["a.NEF", "b.NEF", "c.NEF", "d.NEF", "e.NEF", "f.NEF"]))
    }

    @Test("Replace omits its sources from --skip-sources")
    func testReplaceOmitsTheSourcesFromSkipSources() {
        let skipped = Self.entry(
            negativeID: "n1", overlappingSources: ["a.NEF", "b.NEF", "c.NEF"], groupIndex: 0
        )
        let replaced = Self.entry(
            negativeID: "n2", overlappingSources: ["d.NEF", "e.NEF", "f.NEF"], groupIndex: 1
        )
        var review = OverlapReview(entries: [skipped, replaced])
        review.setDecision(.replace, for: replaced)

        #expect(Set(review.skipSources) == Set(["a.NEF", "b.NEF", "c.NEF"]))
        #expect(!review.skipSources.contains("d.NEF"))
    }

    @Test("The sheet reports how many files Replace would delete")
    func testSheetReportsTheCountOfFilesReplaceWouldDelete() {
        let one = Self.entry(negativeID: "n1", groupIndex: 0)
        let two = Self.entry(negativeID: "n2", groupIndex: 1)
        let three = Self.entry(negativeID: "n3", groupIndex: 2)
        var review = OverlapReview(entries: [one, two, three])
        #expect(review.replaceCount == 0)

        review.setDecision(.replace, for: one)
        #expect(review.replaceCount == 1)

        review.setDecision(.replace, for: two)
        #expect(review.replaceCount == 2)

        review.setDecision(.skip, for: one)
        #expect(review.replaceCount == 1)
    }
}

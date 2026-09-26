import Testing

@testable import ScannyBoy

@Suite("Pluralize")
struct PluralizeTests {
    @Test("counts are singular for one and plural otherwise, never negative(s)")
    func counts() {
        #expect(Pluralize.count(0, "negative") == "0 negatives")
        #expect(Pluralize.count(1, "negative") == "1 negative")
        #expect(Pluralize.count(14, "negative") == "14 negatives")
        #expect(Pluralize.count(1, "file") == "1 file")
    }
}

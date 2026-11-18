import Foundation
import Testing

@testable import ScannyBoy

/// A tiny deterministic generator, so the random-sequence test replays
/// identically on every run.
private struct SeededGenerator: RandomNumberGenerator {
    private var state: UInt64

    init(seed: UInt64) {
        state = seed
    }

    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var value = state
        value = (value ^ (value >> 30)) &* 0xBF58_476D_1CE4_E5B9
        value = (value ^ (value >> 27)) &* 0x94D0_49BB_1331_11EB
        return value ^ (value >> 31)
    }
}

@Suite("Frame log alignment")
struct FrameLogAlignmentTests {
    private static func alignment(_ numbers: [Int?], lastFrame: Int) -> FrameLogAlignment {
        let rows = numbers.enumerated().map {
            FrameLogAlignment.Row(negativeID: "n\($0.offset + 1)", frameNumber: $0.element)
        }
        return FrameLogAlignment(rows: rows, lastFrame: lastFrame)
    }

    private static func numbers(_ alignment: FrameLogAlignment) -> [Int?] {
        alignment.rows.map(\.frameNumber)
    }

    @Test("shifting a row down moves later rows down with it")
    func shiftDown() {
        var alignment = Self.alignment([1, 2, 3, 4], lastFrame: 10)
        alignment.setFrame(row: 1, to: 5)
        #expect(Self.numbers(alignment) == [1, 5, 6, 7])
        alignment.setFrame(row: 1, to: 3)
        #expect(Self.numbers(alignment) == [1, 3, 4, 5])
        #expect(alignment.isValid)
    }

    @Test("shifting a row up pulls later rows up by the same amount")
    func shiftUp() {
        var alignment = Self.alignment([2, 4, 5, 8], lastFrame: 10)
        alignment.setFrame(row: 1, to: 3)
        #expect(Self.numbers(alignment) == [2, 3, 4, 7])
        #expect(alignment.isValid)
    }

    @Test("a shift past the last frame leaves later rows unmatched")
    func shiftPastLastFrame() {
        var alignment = Self.alignment([1, 2, 3, 4], lastFrame: 5)
        alignment.setFrame(row: 1, to: 4)
        #expect(Self.numbers(alignment) == [1, 4, 5, nil])
        alignment.setFrame(row: 0, to: 3)
        #expect(Self.numbers(alignment) == [3, nil, nil, nil])
        #expect(alignment.isValid)
    }

    @Test("matching a previously unmatched row pushes later rows only as needed")
    func setNilRowPushesMinimally() {
        var alignment = Self.alignment([1, nil, 5, 6, 9], lastFrame: 10)
        alignment.setFrame(row: 1, to: 3)
        #expect(Self.numbers(alignment) == [1, 3, 5, 6, 9])

        var tight = Self.alignment([nil, nil, 2, 3], lastFrame: 4)
        tight.setFrame(row: 1, to: 2)
        #expect(Self.numbers(tight) == [nil, 2, 3, 4])
        tight.setFrame(row: 0, to: 1)
        #expect(Self.numbers(tight) == [1, 2, 3, 4])

        var overflow = Self.alignment([nil, 3, 4], lastFrame: 4)
        overflow.setFrame(row: 0, to: 4)
        #expect(Self.numbers(overflow) == [4, nil, nil])
        #expect(overflow.isValid)
    }

    @Test("options start above the nearest matched row and end at the last frame")
    func options() {
        let alignment = Self.alignment([2, nil, 5, nil], lastFrame: 7)
        #expect(alignment.options(forRow: 0) == [1, 2, 3, 4, 5, 6, 7])
        #expect(alignment.options(forRow: 1) == [3, 4, 5, 6, 7])
        #expect(alignment.options(forRow: 2) == [3, 4, 5, 6, 7])
        #expect(alignment.options(forRow: 3) == [6, 7])
        #expect(alignment.options(forRow: 9) == [])
        let full = Self.alignment([nil, 3], lastFrame: 3)
        #expect(full.options(forRow: 1) == [1, 2, 3])
        let exhausted = Self.alignment([3, nil], lastFrame: 3)
        #expect(exhausted.options(forRow: 1) == [])
    }

    @Test("a frame outside the options is ignored")
    func invalidChoiceIgnored() {
        var alignment = Self.alignment([3, 4], lastFrame: 6)
        alignment.setFrame(row: 1, to: 3)
        alignment.setFrame(row: 1, to: 7)
        #expect(Self.numbers(alignment) == [3, 4])
    }

    @Test("no match changes only its own row")
    func noMatch() {
        var alignment = Self.alignment([1, 2, 3], lastFrame: 5)
        alignment.setNoMatch(row: 1)
        #expect(Self.numbers(alignment) == [1, nil, 3])
        #expect(alignment.isValid)
    }

    @Test("reset restores the alignment the CLI proposed")
    func reset() {
        var alignment = Self.alignment([1, 2, 3], lastFrame: 6)
        alignment.setFrame(row: 0, to: 4)
        alignment.setNoMatch(row: 2)
        alignment.reset()
        #expect(Self.numbers(alignment) == [1, 2, 3])
    }

    @Test("skipped counts the numbers jumped since the previous matched row")
    func skipped() {
        let alignment = Self.alignment([3, nil, 4, 8], lastFrame: 9)
        #expect(alignment.skipped(beforeRow: 0) == 2)
        #expect(alignment.skipped(beforeRow: 1) == 0)
        #expect(alignment.skipped(beforeRow: 2) == 0)
        #expect(alignment.skipped(beforeRow: 3) == 3)
    }

    @Test("derived counts and assignments")
    func derivedValues() {
        let alignment = Self.alignment([1, nil, 4], lastFrame: 5)
        #expect(alignment.matchedCount == 2)
        #expect(alignment.unusedFrameNumbers == [2, 3, 5])
        let assignments = alignment.assignments
        #expect(assignments.count == 3)
        #expect(assignments["n1"] == .some(1))
        #expect(assignments["n2"] == Optional<Int?>.some(nil))
        #expect(assignments["n3"] == .some(4))
    }

    @Test("a malformed incoming alignment is cleaned to satisfy the invariant")
    func malformedInput() {
        let alignment = Self.alignment([2, 2, 1, 9, 4], lastFrame: 5)
        #expect(Self.numbers(alignment) == [2, nil, nil, nil, 4])
        #expect(alignment.isValid)
    }

    @Test("the invariant survives random operation sequences")
    func randomOperations() {
        var generator = SeededGenerator(seed: 42)
        for _ in 0..<200 {
            let rowCount = Int.random(in: 1...12, using: &generator)
            let lastFrame = Int.random(in: 1...20, using: &generator)
            var initial: [Int?] = []
            for index in 1...rowCount {
                initial.append(index <= lastFrame ? index : nil)
            }
            var alignment = Self.alignment(initial, lastFrame: lastFrame)
            for _ in 0..<40 {
                let row = Int.random(in: 0..<rowCount, using: &generator)
                switch Int.random(in: 0..<10, using: &generator) {
                case 0:
                    alignment.setNoMatch(row: row)
                case 1:
                    alignment.reset()
                default:
                    if let frame = alignment.options(forRow: row).randomElement(using: &generator) {
                        alignment.setFrame(row: row, to: frame)
                    }
                }
                #expect(alignment.isValid)
                #expect(alignment.rows.count == rowCount)
            }
        }
    }
}

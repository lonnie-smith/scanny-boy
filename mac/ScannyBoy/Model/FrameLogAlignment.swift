import Foundation

/// The pairing of a roll's sequenced negatives with Frame log frame numbers.
///
/// A pure value type. Its invariant: the non-nil frame numbers strictly
/// increase down the rows, and each lies in `1...lastFrame`. Every mutating
/// operation preserves it. See docs/METADATA_IMPORT_PLAN.md section 6.2.
struct FrameLogAlignment: Sendable, Equatable {
    struct Row: Sendable, Equatable {
        let negativeID: String
        var frameNumber: Int?
    }

    /// One row per negative, in sequence order.
    private(set) var rows: [Row]
    let lastFrame: Int
    /// The CLI's alignment, which `reset()` restores.
    private let initialRows: [Row]

    /// Builds from rows, dropping any pairing that would break the
    /// invariant (out of range, or not above the previous kept number), so
    /// a malformed alignment from outside can never break it.
    init(rows: [Row], lastFrame: Int) {
        var cleaned: [Row] = []
        var previous = 0
        for row in rows {
            var kept = row
            if let number = row.frameNumber, number > previous, number <= lastFrame {
                previous = number
            } else {
                kept.frameNumber = nil
            }
            cleaned.append(kept)
        }
        self.rows = cleaned
        self.lastFrame = lastFrame
        initialRows = cleaned
    }

    init(entries: [FrameLogAlignmentEntry], lastFrame: Int) {
        let rows = entries.map { Row(negativeID: $0.negativeID, frameNumber: $0.frameNumber) }
        self.init(rows: rows, lastFrame: lastFrame)
    }

    // MARK: - Queries

    /// The nearest non-nil frame number above `row`, or 0 if there is none.
    private func previousNumber(above row: Int) -> Int {
        var index = row - 1
        while index >= 0 {
            if let number = rows[index].frameNumber { return number }
            index -= 1
        }
        return 0
    }

    /// The frame numbers the picker offers for `row`, besides "No match".
    func options(forRow row: Int) -> [Int] {
        guard rows.indices.contains(row) else { return [] }
        let first = previousNumber(above: row) + 1
        guard first <= lastFrame else { return [] }
        return Array(first...lastFrame)
    }

    /// How many frame numbers were jumped since the previous matched row
    /// (or since the start of the log, for the first matched row). Zero for
    /// an unmatched row.
    func skipped(beforeRow row: Int) -> Int {
        guard rows.indices.contains(row), let number = rows[row].frameNumber else { return 0 }
        return max(0, number - previousNumber(above: row) - 1)
    }

    /// The frame numbers in `1...lastFrame` that no row uses, ascending.
    var unusedFrameNumbers: [Int] {
        guard lastFrame >= 1 else { return [] }
        let used = Set(rows.compactMap(\.frameNumber))
        return (1...lastFrame).filter { !used.contains($0) }
    }

    var matchedCount: Int {
        rows.filter { $0.frameNumber != nil }.count
    }

    /// `{negativeID: frameNumber | nil}` covering every row, as
    /// `framelog apply` takes it.
    var assignments: [String: Int?] {
        Dictionary(uniqueKeysWithValues: rows.map { ($0.negativeID, $0.frameNumber) })
    }

    /// True when the invariant holds. Always true unless there is a bug;
    /// the unit tests check it after random operation sequences.
    var isValid: Bool {
        var previous = 0
        for row in rows {
            guard let number = row.frameNumber else { continue }
            guard number > previous, number <= lastFrame else { return false }
            previous = number
        }
        return true
    }

    // MARK: - Operations

    /// Pairs `row` with frame `frame` (which must be one of its options;
    /// anything else is ignored). Later rows follow: when the row already
    /// had a number, each later matched row shifts by the same amount; when
    /// it had none, later rows are pushed forward only as far as needed.
    /// Anything pushed above `lastFrame` becomes unmatched.
    mutating func setFrame(row: Int, to frame: Int) {
        guard rows.indices.contains(row), options(forRow: row).contains(frame) else { return }
        let old = rows[row].frameNumber
        rows[row].frameNumber = frame
        let next = row + 1
        guard next < rows.count else { return }
        if let old {
            let delta = frame - old
            for index in next..<rows.count {
                guard let number = rows[index].frameNumber else { continue }
                let shifted = number + delta
                rows[index].frameNumber = shifted <= lastFrame ? shifted : nil
            }
        } else {
            var previous = frame
            for index in next..<rows.count {
                guard let number = rows[index].frameNumber else { continue }
                var pushed = number
                if pushed <= previous { pushed = previous + 1 }
                if pushed <= lastFrame {
                    rows[index].frameNumber = pushed
                    previous = pushed
                } else {
                    rows[index].frameNumber = nil
                }
            }
        }
    }

    /// Leaves `row` unmatched; every other row is unchanged.
    mutating func setNoMatch(row: Int) {
        guard rows.indices.contains(row) else { return }
        rows[row].frameNumber = nil
    }

    /// Restores the alignment the CLI proposed.
    mutating func reset() {
        rows = initialRows
    }
}

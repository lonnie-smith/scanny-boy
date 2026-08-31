import SwiftUI

/// Section 3.4/3.5: before a run whose selection overlaps sources already
/// in the roll, one row per overlapping prospective negative with a
/// Skip/Replace toggle, defaulting to Skip. Non-overlapping groups never
/// appear here — they always run, whatever this sheet decides.
///
/// Replace must say what it does: choosing it supersedes the named negative
/// and deletes its published TIFF, so each row names that negative and the
/// footer states how many files Replace, as currently decided, would
/// delete. Skip is the default precisely because Replace is destructive.
struct OverlapSheet: View {
    let entries: [RollOverlapEntry]
    /// Called with the `--skip-sources` list the sheet's decisions imply,
    /// right before the sheet dismisses.
    let onConfirm: ([String]) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var review: OverlapReview

    init(entries: [RollOverlapEntry], onConfirm: @escaping ([String]) -> Void) {
        self.entries = entries
        self.onConfirm = onConfirm
        self._review = State(initialValue: OverlapReview(entries: entries))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Overlapping Negatives").font(.title2.bold())
            Text(
                "This selection overlaps \(entries.count) negative(s) already in the roll. "
                    + "Replace supersedes the named negative and deletes its TIFF; "
                    + "Skip leaves it alone."
            )
            .font(.callout)
            .foregroundStyle(.secondary)

            List(entries, id: \.reviewKey) { entry in
                row(for: entry)
            }
            .listStyle(.inset)
            .frame(minHeight: 160)

            if review.replaceCount > 0 {
                Text("Replace will delete \(review.replaceCount) file(s).")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Start Run") {
                    onConfirm(review.skipSources)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 480, minHeight: 320)
    }

    @ViewBuilder
    private func row(for entry: RollOverlapEntry) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.expectedOutput).font(.body)
                Text(entry.overlappingSources.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Picker(
                "",
                selection: Binding(
                    get: { review.decision(for: entry) },
                    set: { review.setDecision($0, for: entry) }
                )
            ) {
                Text("Skip").tag(OverlapReview.Decision.skip)
                Text("Replace").tag(OverlapReview.Decision.replace)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 160)
        }
    }
}

/// The overlap sheet's per-row decisions, and what they mean for
/// `--skip-sources` (section 3.4/3.5) — kept as a plain, independently
/// testable type rather than folded into `@State` alone, so the Skip/Replace
/// rule can be tested without driving a live view.
struct OverlapReview: Sendable, Hashable {
    enum Decision: Sendable, Hashable {
        case skip
        case replace
    }

    let entries: [RollOverlapEntry]
    private var decisions: [String: Decision]

    /// Skip is the default (section 3.5): Replace is destructive, so
    /// nothing supersedes anything until the user says so.
    init(entries: [RollOverlapEntry]) {
        self.entries = entries
        self.decisions = Dictionary(uniqueKeysWithValues: entries.map { ($0.reviewKey, .skip) })
    }

    func decision(for entry: RollOverlapEntry) -> Decision {
        decisions[entry.reviewKey] ?? .skip
    }

    mutating func setDecision(_ decision: Decision, for entry: RollOverlapEntry) {
        decisions[entry.reviewKey] = decision
    }

    /// Every overlapping source of every entry still marked Skip — passed to
    /// `run` as `--skip-sources`, which removes them from the selection
    /// before grouping (section 3.5). An entry marked Replace omits its
    /// sources here, so its group runs and, per section 3.4, supersedes the
    /// negative it overlapped.
    var skipSources: [String] {
        entries
            .filter { decision(for: $0) == .skip }
            .flatMap { $0.overlappingSources }
    }

    /// How many existing TIFFs Replace, as currently decided, would delete —
    /// one per replaced negative (section 3.4).
    var replaceCount: Int {
        entries.filter { decision(for: $0) == .replace }.count
    }
}

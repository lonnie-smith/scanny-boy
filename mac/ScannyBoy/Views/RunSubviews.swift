import AppKit
import SwiftUI

/// Live progress for a conversion in flight: the pipeline step, the file it
/// names, and how many negatives have been completed so far.
///
/// The bar is driven by `fractionComplete`, which comes from
/// `negativesCompleted`/`totalNegatives` — never from a source index
/// (section 4.2) and never from elapsed time, which section 4.2's
/// per-negative durations vary too much to extrapolate reliably.
struct RunProgressView: View {
    let run: RunModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let fraction = run.fractionComplete {
                ProgressView(value: fraction)
            } else {
                ProgressView()
            }

            HStack {
                Text(stepDescription)
                Spacer()
                if let totalNegatives = run.totalNegatives {
                    Text("\(run.negativesCompleted) of \(totalNegatives) negative(s)")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private var stepDescription: String {
        let step = run.currentStep.map(RunStepName.string) ?? "Starting"
        guard let filename = run.currentFilename else { return step }
        return "\(step) — \(filename)"
    }
}

/// Section 4.2's pipeline steps, in words.
enum RunStepName {
    static func string(_ step: CLIPipelineStep) -> String {
        switch step {
        case .decode: "Decoding"
        case .writeTIFF: "Writing TIFF"
        case .addMetadata: "Adding metadata"
        case .load: "Loading intermediates"
        case .detect: "Detecting features"
        case .match: "Registering frames"
        case .solve: "Solving layout"
        case .warp: "Warping frames"
        case .blend: "Blending"
        case .normalize: "Normalizing"
        case .writeStitched: "Writing stitched TIFF"
        case .unknown(let name): name
        }
    }
}

/// Shown for `RunModel.Phase.finishing`: the helper has exited and the
/// manifest read-back is in flight, which on a large roll is not instant
/// (M10). Distinct from `RunProgressView` — there is no more progress to
/// show, and Cancel has nothing left to cancel.
struct FinishingView: View {
    var body: some View {
        HStack {
            ProgressView().controlSize(.small)
            Text("Finishing…")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

/// What a run left behind: one row per negative, with failures and
/// attributed warnings inside an expandable detail rather than as parallel
/// lists, plus the run-level notes that belong to no single negative.
struct RunResultView: View {
    let run: RunModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let summary = run.completionSummary {
                Text(summary).font(.callout)
            }
            countsRow

            // A stream failure is not a CLI error, and neither is shown as
            // the other (Chunk 10).
            ForEach(Array(run.streamFailures.enumerated()), id: \.offset) { _, failure in
                Label(
                    "Stream problem — \(RunFailureText.string(failure))",
                    systemImage: "antenna.radiowaves.left.and.right.slash"
                )
                .font(.caption)
                .foregroundStyle(.red)
            }

            ForEach(run.negativeResults) { result in
                NegativeResultRow(
                    result: result,
                    initiallyExpanded: isInteresting(result)
                )
            }

            runLevelWarnings

            if let report = run.manifestReport {
                Text(report.summary)
                    .font(.caption)
                    .foregroundStyle(isCleanupIncomplete(report) ? .orange : .secondary)
            }
            if let report = run.rollManifestReport {
                Text(report.summary)
                    .font(.caption)
                    .foregroundStyle(isRollCleanupIncomplete(report) ? .orange : .secondary)
            }

            copyReportButton
        }
    }

    private func isCleanupIncomplete(_ report: ManifestReport) -> Bool {
        if case .cleanupIncomplete = report { return true }
        return false
    }

    private func isRollCleanupIncomplete(_ report: RollManifestReport) -> Bool {
        if case .cleanupIncomplete = report { return true }
        return false
    }

    /// Failures and skips are what a user is scanning for, so those rows
    /// start expanded; a clean roll with few negatives shows everything,
    /// and a long clean roll collapses to one line per negative.
    private func isInteresting(_ result: RunModel.NegativeResult) -> Bool {
        result.status != .succeeded || run.negativeResults.count <= 3
    }

    @ViewBuilder
    private var countsRow: some View {
        if !countsText.isEmpty {
            Text(countsText)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var countsText: String {
        let results = run.negativeResults
        let converted = results.filter { $0.status == .succeeded }.count
        let failed = results.filter { $0.status == .failed }.count
        let skipped = results.filter { $0.status == .skipped }.count
        var parts: [String] = []
        if converted > 0 { parts.append("\(converted) converted") }
        if failed > 0 { parts.append("\(failed) failed") }
        if skipped > 0 { parts.append("\(skipped) skipped") }
        let warningCount = run.warnings.count
        if warningCount > 0 {
            parts.append("\(warningCount) warning\(warningCount == 1 ? "" : "s")")
        }
        return parts.joined(separator: ", ")
    }

    /// Warnings no negative lays claim to, deduplicated by code and message
    /// so a repeated condition is one row with a count.
    @ViewBuilder
    private var runLevelWarnings: some View {
        let grouped = Dictionary(grouping: run.runLevelWarnings, by: \.self)
        ForEach(Array(grouped.values.sorted { $0.count > $1.count }), id: \.self) { group in
            if let warning = group.first {
                Label(
                    group.count > 1
                        ? "\(warning.message) (\(group.count)×)"
                        : warning.message,
                    systemImage: "exclamationmark.triangle"
                )
                .font(.caption)
                .foregroundStyle(.orange)
            }
        }
    }

    private var copyReportButton: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(run.reportText, forType: .string)
        } label: {
            Label("Copy Report", systemImage: "doc.on.doc")
        }
        .font(.caption)
    }
}

/// One negative's result: a status icon and the output filename (or the
/// negative's id) collapsed, with the failure, attributed warnings, and
/// exact quality numbers in the expandable detail.
private struct NegativeResultRow: View {
    let result: RunModel.NegativeResult
    private let initiallyExpanded: Bool

    @State private var isExpanded: Bool

    /// The initial expansion is decided once, at row creation; after that
    /// the user owns the disclosure.
    init(result: RunModel.NegativeResult, initiallyExpanded: Bool) {
        self.result = result
        self.initiallyExpanded = initiallyExpanded
        _isExpanded = State(initialValue: initiallyExpanded)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            detail
                .padding(.leading, 8)
        } label: {
            Label(collapsedTitle, systemImage: statusSystemImage)
                .foregroundStyle(statusColor)
                .font(.caption)
        }
    }

    private var collapsedTitle: String {
        result.output ?? result.id
    }

    private var statusSystemImage: String {
        switch result.status {
        case .succeeded: "checkmark.circle"
        case .failed: "xmark.octagon"
        case .skipped: "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch result.status {
        case .succeeded: .green
        case .failed: .red
        case .skipped: .orange
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let failure = result.failure {
            VStack(alignment: .leading, spacing: 2) {
                Label(
                    failure.code.friendlyTitle ?? failure.code.name,
                    systemImage: "xmark.octagon"
                )
                .font(.caption)
                .foregroundStyle(.red)
                Text(failure.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(failure.code.name)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }

        ForEach(result.warnings, id: \.self) { warning in
            // The row already names the negative; drop the message's
            // "{negative_id}: " prefix.
            Label(
                stripIDPrefix(warning.message),
                systemImage: "exclamationmark.triangle"
            )
            .font(.caption)
            .foregroundStyle(.orange)
        }

        if let quality = result.quality {
            Text(qualityLine(quality))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func qualityLine(_ quality: RunModel.NegativeResult.Quality) -> String {
        let word: String
        switch quality {
        case .good: word = "good"
        case .fair: word = "fair"
        case .poor: word = "poor"
        }
        var line = "Alignment: \(word)"
        if let dimensions = result.dimensions {
            line += " — \(dimensions)"
        }
        if let detail = result.qualityDetail {
            line += ", \(detail)"
        }
        return line
    }

    private func stripIDPrefix(_ message: String) -> String {
        guard message.hasPrefix("\(result.id): ") else { return message }
        return String(message.dropFirst(result.id.count + 2))
    }
}

enum RunFailureText {
    static func string(_ failure: CLISessionFailure) -> String {
        switch failure {
        case .launch(let message):
            "the helper could not be launched: \(message)"
        case .read(let stream, let message):
            "\(stream.rawValue) could not be read: \(message)"
        case .decode(let line, let reason):
            "a line of output was not a usable event (\(reason)): \(line)"
        }
    }
}


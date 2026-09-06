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

/// What a run left behind: one line per negative naming whether its output
/// file was emitted, plus the run-level notes that belong to no single
/// negative. Per-negative diagnostics live on the Edit tab.
struct RunResultView: View {
    let run: RunModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let summary = run.completionSummary {
                Text(summary).font(.callout)
            }

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
                EmissionResultRow(result: result)
            }

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

/// One negative's emission status: the output filename (or the negative's id)
/// and whether it was published.
private struct EmissionResultRow: View {
    let result: RunModel.NegativeResult

    var body: some View {
        Text("\(label) — \(statusWord)")
            .font(.caption)
            .foregroundStyle(isFailure ? .red : .primary)
    }

    private var label: String {
        result.output ?? result.id
    }

    private var statusWord: String {
        switch result.status {
        case .succeeded: "emitted"
        case .failed: "failed"
        case .skipped: "skipped"
        }
    }

    private var isFailure: Bool {
        result.status != .succeeded
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

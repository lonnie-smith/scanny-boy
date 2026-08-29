import AppKit
import SwiftUI

/// Live progress for a conversion in flight: the pipeline step, the file it
/// names, the completed count, elapsed time, and estimated remaining time.
///
/// The bar is driven by `fractionComplete`, which comes from `progress`'s
/// `completed`/`total` counts — never from a source index (section 4.2).
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
                Text("\(run.completedSteps) of \(run.totalSteps) steps")
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            HStack {
                Text("Elapsed \(RunTimeFormat.string(run.elapsed))")
                Spacer()
                if let remaining = run.estimatedRemaining {
                    Text("About \(RunTimeFormat.string(remaining)) remaining")
                } else {
                    Text("Estimating…")
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
        case .unknown(let name): name
        }
    }
}

enum RunTimeFormat {
    static func string(_ interval: TimeInterval) -> String {
        let total = Int(interval.rounded())
        let minutes = total / 60
        let seconds = total % 60
        return minutes > 0 ? "\(minutes)m \(seconds)s" : "\(seconds)s"
    }
}

/// What a run left behind: which negatives finished, which failed, what was
/// published, and what the manifest says about the state of the folder.
struct RunResultView: View {
    let run: RunModel
    let outputFolder: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let summary = run.completionSummary {
                Text(summary).font(.callout)
            }

            if !run.completedGroups.isEmpty {
                Label(
                    "Completed: \(run.completedGroups.joined(separator: ", "))",
                    systemImage: "checkmark.circle"
                )
                .font(.caption)
                .foregroundStyle(.green)
            }

            ForEach(run.failedGroups, id: \.self) { group in
                Label(
                    "\(group.groupID) failed — \(group.code.name): \(group.message)",
                    systemImage: "xmark.octagon"
                )
                .font(.caption)
                .foregroundStyle(.red)
            }

            ForEach(run.warnings, id: \.self) { warning in
                Label(warning.message, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
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

            if let report = run.manifestReport {
                Text(report.summary)
                    .font(.caption)
                    .foregroundStyle(isCleanupIncomplete(report) ? .orange : .secondary)
            }

            if !run.publishedOutputs.isEmpty {
                Text("Published: \(run.publishedOutputs.joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }

            if let outputFolder {
                Button("Reveal in Finder") {
                    RunFinderReveal.reveal(
                        outputFolder: outputFolder, published: run.publishedOutputs
                    )
                }
            }
        }
    }

    private func isCleanupIncomplete(_ report: ManifestReport) -> Bool {
        if case .cleanupIncomplete = report { return true }
        return false
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

enum RunFinderReveal {
    /// Selects the run's published files when there are any, and otherwise
    /// just opens the output folder.
    static func reveal(outputFolder: URL, published: [String]) {
        let urls = published.map {
            outputFolder.appending(path: $0, directoryHint: .notDirectory)
        }
        if urls.isEmpty {
            NSWorkspace.shared.activateFileViewerSelecting([outputFolder])
        } else {
            NSWorkspace.shared.activateFileViewerSelecting(urls)
        }
    }
}

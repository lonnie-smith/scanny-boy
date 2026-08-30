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
        case .load: "Loading intermediates"
        case .detect: "Detecting features"
        case .match: "Registering frames"
        case .solve: "Solving layout"
        case .warp: "Warping frames"
        case .blend: "Blending"
        case .writeStitched: "Writing stitched TIFF"
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
    /// Called with the kept work directory's path when the user asks to
    /// re-stitch it (Chunk P2-10's "button"). `ContentView` owns actually
    /// presenting the sheet.
    let onRestitch: (String) -> Void

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

            // One row per stitched negative, with the section 3.4 quality
            // numbers it was published with.
            ForEach(run.stitchedNegatives, id: \.self) { negative in
                Label(
                    "\(negative.output) — \(negative.width)×\(negative.height), "
                        + "RMS \(String(format: "%.2f", negative.globalRMS))px, "
                        + "overlap MAD \(String(format: "%.3f", negative.maxOverlapMAD))",
                    systemImage: "checkmark.circle"
                )
                .font(.caption)
                .foregroundStyle(.green)
            }

            ForEach(run.failedNegatives, id: \.self) { negative in
                Label(
                    "\(negative.groupID) failed — \(negative.code.name): \(negative.message)",
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
            if let report = run.rollManifestReport {
                Text(report.summary)
                    .font(.caption)
                    .foregroundStyle(isRollCleanupIncomplete(report) ? .orange : .secondary)
            }

            // `run`/`stitch` published stitched negatives; a plain `convert`
            // published per-frame TIFFs. Never both, so this is unambiguous.
            if !run.stitchedNegatives.isEmpty {
                Text("Published: " + run.stitchedNegatives.map(\.output).joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            } else if !run.publishedOutputs.isEmpty {
                Text("Published: \(run.publishedOutputs.joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }

            if let outputFolder {
                Button("Reveal in Finder") {
                    let published = run.stitchedNegatives.isEmpty
                        ? run.publishedOutputs
                        : run.stitchedNegatives.map(\.output)
                    RunFinderReveal.reveal(outputFolder: outputFolder, published: published)
                }
            }

            // Section 3.5: the work directory survives whenever a negative
            // failed, the run was cancelled, or intermediates were asked to
            // be kept — this is how the app finds it again, most usefully
            // for Chunk P2-10's re-stitch.
            if let keptWorkDirectory = run.keptWorkDirectory {
                HStack {
                    Button("Open Kept Work Directory") {
                        NSWorkspace.shared.open(URL(filePath: keptWorkDirectory))
                    }
                    Button("Re-stitch…") { onRestitch(keptWorkDirectory) }
                }
            }
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

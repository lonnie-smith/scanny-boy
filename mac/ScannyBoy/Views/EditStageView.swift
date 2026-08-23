import AppKit
import SwiftUI

/// Chunk P3-12's Edit tab (section 3.10): the selected roll's negatives in
/// sequence order with thumbnails, source frames, and quality metrics; the
/// dirty count and Apply; the roll's name and folder path.
///
/// The roll capture date, each negative's date override, and
/// `shots_per_negative` are shown read-only. No CLI command exists yet to
/// write `metadata.roll_capture_date`, a negative's
/// `capture_time.date_override`, or an unlocked roll's
/// `shots_per_negative` (sections 3.5/3.7/3.8 describe what these fields
/// mean but not how the app is meant to set them) — a decision made
/// stopping to ask mid-chunk, recorded rather than guessed at. A later
/// chunk that adds the CLI writes for them can turn these into real
/// controls.
struct EditStageView: View {
    @Bindable var edit: EditModel
    let run: RunModel

    var body: some View {
        Form {
            rollSection
            negativesSection
            applySection
        }
        .formStyle(.grouped)
        .padding()
        .disabled(run.isActive)
        // `initial: true` matters: a run usually finishes while this tab is
        // not mounted (runs are started from Add Scans), so the phase can
        // already be `.finished` when the tab first appears — and that is
        // exactly when the pre-run manifest it would otherwise show is
        // stale. Apply just finished — the dirty count and applied
        // timestamps it changed are only known once the roll is re-read.
        .onChange(of: run.phase, initial: true) { _, phase in
            if phase == .finished { edit.refresh() }
        }
    }

    private var rollSection: some View {
        Section("Roll") {
            if let roll = edit.roll {
                LabeledContent("Name", value: roll.rollName)
                LabeledContent("Shots per negative", value: String(roll.shotsPerNegative))
                LabeledContent("Capture date", value: roll.metadata.rollCaptureDate ?? "Not set")
            }
            if let rollURL = edit.rollURL {
                LabeledContent("Folder") {
                    HStack {
                        Text(rollURL.path)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button("Open") {
                            NSWorkspace.shared.activateFileViewerSelecting([rollURL])
                        }
                    }
                }
            }
        }
    }

    private var negativesSection: some View {
        Section("Negatives") {
            if edit.visibleNegatives.isEmpty {
                Text("No negatives yet.").foregroundStyle(.secondary)
            } else {
                ForEach(edit.visibleNegatives, id: \.negativeID) { negative in
                    NegativeRow(negative: negative, rollURL: edit.rollURL)
                }
            }
        }
    }

    private var applySection: some View {
        Section("Metadata") {
            HStack {
                Text("\(edit.dirtyCount) negative(s) need their capture time written")
                Spacer()
                Button("Apply") { applyMetadata() }
                    .disabled(!edit.canApply || run.isActive)
            }
            if run.phase == .finished, let summary = run.completionSummary,
                run.stitchedNegatives.isEmpty, !run.appliedNegativeIDs.isEmpty
                    || !run.skippedMetadata.isEmpty
            {
                Text(summary).font(.caption).foregroundStyle(.secondary)
                ForEach(run.skippedMetadata, id: \.groupID) { skipped in
                    IssueLabel(
                        issue: ConfigurationModel.Issue(code: skipped.code, message: skipped.message),
                        style: .warning
                    )
                }
            }
        }
    }

    private func applyMetadata() {
        guard let command = edit.applyCommand, let rollURL = edit.rollURL else { return }
        run.start(
            command: command, files: [], outputFolder: rollURL, totalNegatives: edit.dirtyCount
        )
        Task {
            await run.waitForCompletion()
            edit.refresh()
        }
    }
}

private struct NegativeRow: View {
    let negative: RollManifest.Negative
    let rollURL: URL?

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?

    private static let thumbnailSize = CGSize(width: 80, height: 80)

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            preview
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text(negative.expectedOutput).font(.body)
                Text("Frames: \(negative.members.joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let rms = negative.globalRMSPixels {
                    Text("Global RMS: \(rms, specifier: "%.3f") px")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let rebate = negative.rebateDeviationPixels {
                    Text("Rebate deviation: \(rebate, specifier: "%.3f") px")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text("Status: \(negative.status)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .task(id: outputURL) {
            guard let outputURL else { return }
            thumbnail = await ThumbnailLoader.shared.thumbnail(
                forStitchedTIFF: outputURL,
                pointSize: Self.thumbnailSize,
                scale: displayScale
            )
        }
    }

    private var outputURL: URL? {
        guard let rollURL, let name = negative.output?.name else { return nil }
        return rollURL.appending(path: name, directoryHint: .notDirectory)
    }

    @ViewBuilder
    private var preview: some View {
        if let thumbnail {
            Image(nsImage: thumbnail.image)
                .resizable()
                .interpolation(.medium)
                .aspectRatio(contentMode: .fit)
        } else {
            RoundedRectangle(cornerRadius: 4)
                .fill(.quaternary)
                .overlay {
                    Image(systemName: "photo")
                        .foregroundStyle(.secondary)
                }
        }
    }
}

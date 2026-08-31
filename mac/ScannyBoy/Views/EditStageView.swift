import SwiftUI

/// Chunk P3-12's Edit tab (section 3.10): the selected roll's negatives in
/// sequence order with thumbnails, source frames, and quality metrics; the
/// dirty count and Apply; the roll's name, folder path, and run history.
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
            runHistorySection
        }
        .formStyle(.grouped)
        .padding()
        .disabled(run.isActive)
        .onChange(of: run.phase) { _, phase in
            // Apply just finished — the dirty count and applied timestamps
            // it changed are only known once the roll is re-read.
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
                LabeledContent("Folder", value: rollURL.path)
                    .font(.caption)
            }
        }
    }

    private var negativesSection: some View {
        Section("Negatives") {
            Toggle("Show replaced negatives", isOn: $edit.showSupersededNegatives)
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

    private var runHistorySection: some View {
        Section("Run history") {
            if let runs = edit.roll?.runs, !runs.isEmpty {
                ForEach(runs, id: \.runID) { historyRun in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(historyRun.kind) — \(historyRun.status)")
                        Text(historyRun.startedAt)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("No runs yet.").foregroundStyle(.secondary)
            }
        }
    }

    private func applyMetadata() {
        guard let command = edit.applyCommand, let rollURL = edit.rollURL else { return }
        run.start(command: command, files: [], outputFolder: rollURL)
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
                if let supersededBy = negative.supersededBy {
                    Text("Replaced by \(supersededBy)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Text("Status: \(negative.status)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .opacity(negative.isSuperseded ? 0.5 : 1)
        .disabled(negative.isSuperseded)
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

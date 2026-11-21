import SwiftUI

/// Picks any negative already in the roll as a re-scan target (TETHER_PLAN
/// §4.5). The list is the roll's own record from `roll info`, in the order
/// the CLI sequenced it; negatives that never published (no sequence) follow.
struct RescanPickerSheet: View {
    let runner: CLIRunner
    let rollURL: URL
    /// Where the roll's tethered frames live; the target's old frames are
    /// named from it, and only files there are ever moved to the Trash.
    let captureFolder: URL?
    /// Whether a re-scan of this `negative_id` is already queued.
    let isPending: (String) -> Bool
    let onPick: (CaptureSessionModel.RescanTarget) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var manifest: RollManifest?
    @State private var loadFailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Re-scan a negative").font(.title2.bold())
            Text(
                "The new capture takes this negative's place in the roll and its file name. "
                    + "Its metadata is kept; its edits are reset."
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            content
                .frame(minHeight: 240)
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 460)
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if let manifest {
            if manifest.negatives.isEmpty {
                ContentUnavailableView("No negatives yet", systemImage: "film")
            } else {
                List(ordered(manifest.negatives), id: \.negativeID) { negative in
                    row(negative)
                }
            }
        } else if loadFailed {
            ContentUnavailableView("Could not read the roll", systemImage: "exclamationmark.triangle")
        } else {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func row(_ negative: RollManifest.Negative) -> some View {
        HStack(spacing: 10) {
            RescanPickerThumbnail(rollURL: rollURL, outputName: negative.output?.name)
            VStack(alignment: .leading, spacing: 2) {
                Text(displayName(negative)).font(.body.monospacedDigit())
                Text(caption(negative)).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Re-scan") {
                onPick(target(for: negative))
                dismiss()
            }
            .disabled(isPending(negative.negativeID))
            .help(
                isPending(negative.negativeID)
                    ? "A re-scan of this negative is already queued" : ""
            )
        }
    }

    /// The CLI's sequence order (published negatives), then the rest in
    /// record order. Display only — `sequence` is the CLI's own number.
    private func ordered(_ negatives: [RollManifest.Negative]) -> [RollManifest.Negative] {
        let sequenced = negatives.filter { $0.sequence != nil }
            .sorted { ($0.sequence ?? 0) < ($1.sequence ?? 0) }
        return sequenced + negatives.filter { $0.sequence == nil }
    }

    private func displayName(_ negative: RollManifest.Negative) -> String {
        negative.output?.name ?? negative.expectedOutput
    }

    private func caption(_ negative: RollManifest.Negative) -> String {
        if let sequence = negative.sequence {
            return "#\(sequence) · \(negative.members.count) frames"
        }
        return "\(negative.status.capitalized) · \(negative.members.count) frames"
    }

    private func target(for negative: RollManifest.Negative) -> CaptureSessionModel.RescanTarget {
        CaptureSessionModel.RescanTarget(
            displayName: displayName(negative),
            rollNegativeID: negative.negativeID,
            queueEntryID: nil,
            oldFramePaths: captureFolder.map { folder in
                negative.members.map { folder.appending(path: $0).path }
            } ?? []
        )
    }

    private func load() async {
        manifest = await EditModel.fetchRollManifest(runner: runner, roll: rollURL)
        loadFailed = manifest == nil
    }
}

private struct RescanPickerThumbnail: View {
    static let size = CGSize(width: 54, height: 36)
    let rollURL: URL
    let outputName: String?
    @State private var thumbnail: Thumbnail?

    var body: some View {
        Group {
            if let thumbnail {
                Image(nsImage: thumbnail.image).resizable().scaledToFit()
            } else {
                Image(systemName: outputName == nil ? "xmark.octagon" : "photo")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .clipShape(RoundedRectangle(cornerRadius: 3))
        .task(id: outputName) {
            guard let outputName else { return }
            thumbnail = await ThumbnailLoader.shared.thumbnail(
                forStitchedTIFF: rollURL.appending(path: outputName),
                pointSize: Self.size,
                scale: NSScreen.main?.backingScaleFactor ?? 2
            )
        }
    }
}

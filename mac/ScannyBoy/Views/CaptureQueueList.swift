import SwiftUI

/// One row per negative in the capture queue, showing status, progress, and
/// elapsed time. Replaces the old tile-based strip.
struct CaptureQueueList: View {
    static let maxVisibleRows = 5

    let stitchQueue: StitchQueueModel
    /// The roll being captured; the queue holds other rolls' entries too.
    let rollURL: URL?

    var body: some View {
        // Rows are a fixed height; the list scrolls past `maxVisibleRows`
        // rather than squeezing rows into a shorter frame, where they overlap.
        ScrollView(.vertical) {
            LazyVStack(spacing: 0) {
                ForEach(negatives) { negative in
                    CaptureQueueRow(
                        negative: negative,
                        progress: stitchQueue.progress[negative.id],
                        rollURL: rollURL
                    )
                }
            }
        }
        .frame(
            height: CGFloat(min(negatives.count, Self.maxVisibleRows)) * CaptureQueueRow.rowHeight
        )
    }

    /// Newest first — the negative just shot is at the top.
    private var negatives: [StitchQueueModel.QueuedNegative] {
        stitchQueue.negatives(for: rollURL).reversed()
    }
}

struct CaptureQueueRow: View {
    static let rowHeight: CGFloat = 38
    static let thumbnailSize = CGSize(width: 48, height: 32)

    let negative: StitchQueueModel.QueuedNegative
    let progress: StitchQueueModel.StepProgress?
    let rollURL: URL?

    @State private var thumbnail: Thumbnail?

    var body: some View {
        HStack(spacing: 8) {
            leadingIcon
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
            Text(negative.stamp)
                .font(.caption.monospacedDigit())
                .fixedSize()
                .frame(minWidth: 110, alignment: .leading)
            statusLabel
                .font(.caption)
                .lineLimit(1)
                .frame(minWidth: 100, alignment: .leading)
            if showProgress {
                progressBar
                    .frame(width: 80)
            }
            Spacer()
            trailingLabel
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 50, alignment: .trailing)
        }
        .padding(.horizontal, 8)
        .frame(height: Self.rowHeight)
        .opacity(failedRow ? 0.7 : 1.0)
        .help(helpText)
        .task(id: negative.outputFilename) {
            await loadThumbnail()
        }
    }

    // MARK: - Leading icon / thumbnail

    @ViewBuilder
    private var leadingIcon: some View {
        if negative.step == .published, let thumbnail {
            Image(nsImage: thumbnail.image)
                .resizable()
                .scaledToFit()
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
                .clipShape(RoundedRectangle(cornerRadius: 3))
        } else if negative.step == .published {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        } else {
            Image(systemName: iconName)
                .foregroundStyle(iconColor)
        }
    }

    private var iconName: String {
        switch negative.step {
        case .published: "checkmark.circle.fill"
        case .preparing, .checking, .stitching: "gearshape.fill"
        case .prepareFailed, .checkFailed, .stitchFailed: "xmark.octagon.fill"
        case .waitingForDisk: "externaldrive.badge.exclamationmark"
        case .waitingPrepare, .waitingCheck, .waitingStitch: "clock.fill"
        }
    }

    private var iconColor: Color {
        switch negative.step {
        case .published: .green
        case .prepareFailed, .checkFailed, .stitchFailed: .red
        case .stitching, .preparing, .checking: .orange
        case .waitingForDisk: .yellow
        default: .secondary
        }
    }

    // MARK: - Status text

    private var statusLabel: some View {
        Text(Self.statusText(for: negative, progress: progress))
    }

    static func statusText(
        for negative: StitchQueueModel.QueuedNegative,
        progress: StitchQueueModel.StepProgress?
    ) -> String {
        let stepName: String
        switch negative.step {
        case .waitingPrepare: return "Waiting to prepare"
        case .preparing:
            stepName = "Preparing"
        case .waitingCheck: return "Waiting to check"
        case .checking:
            stepName = "Checking"
        case .waitingStitch: return "Waiting to stitch"
        case .stitching:
            stepName = "Stitching"
        case .published: return "Published"
        case .prepareFailed:
            return "Prepare failed — \(negative.failureMessage ?? "unknown")"
        case .checkFailed:
            return "Check failed — \(negative.failureMessage ?? "unknown")"
        case .stitchFailed:
            return "Stitch failed — \(negative.failureMessage ?? "unknown")"
        case .waitingForDisk: return "Waiting for disk space"
        }
        if let step = progress?.step {
            return "\(stepName) · \(RunStepName.string(step))"
        }
        return stepName
    }

    // MARK: - Progress bar

    private var showProgress: Bool {
        switch negative.step {
        case .preparing, .checking, .stitching: true
        default: false
        }
    }

    @ViewBuilder
    private var progressBar: some View {
        if let progress, progress.total > 0 {
            ProgressView(value: Double(progress.completed), total: Double(progress.total))
                .progressViewStyle(.linear)
        } else {
            ProgressView()
                .progressViewStyle(.linear)
        }
    }

    // MARK: - Trailing label

    @ViewBuilder
    private var trailingLabel: some View {
        switch negative.step {
        case .published:
            if let published = negative.publishedAt {
                Text(Self.formatDuration(published.timeIntervalSince(negative.enqueuedAt)))
            }
        case .preparing, .checking, .stitching:
            elapsedView
        default:
            EmptyView()
        }
    }

    @ViewBuilder
    private var elapsedView: some View {
        if let progress {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let elapsed = context.date.timeIntervalSince(progress.startedAt)
                Text(Self.formatDuration(elapsed))
            }
        }
    }

    private var failedRow: Bool {
        switch negative.step {
        case .prepareFailed, .checkFailed, .stitchFailed: true
        default: false
        }
    }

    // MARK: - Thumbnail

    private func loadThumbnail() async {
        guard negative.step == .published,
            let outputFilename = negative.outputFilename,
            let rollURL
        else { return }
        let tiffURL = rollURL.appending(path: outputFilename)
        let scale = NSScreen.main?.backingScaleFactor ?? 2
        thumbnail = await ThumbnailLoader.shared.thumbnail(
            forStitchedTIFF: tiffURL,
            pointSize: Self.thumbnailSize,
            scale: scale
        )
    }

    // MARK: - Help

    private var helpText: String {
        if let message = negative.failureMessage {
            return message
        }
        return negative.step.rawValue
    }

    // MARK: - Formatting

    static func formatDuration(_ interval: TimeInterval) -> String {
        guard interval.isFinite, interval >= 0 else { return "" }
        let totalSeconds = Int(interval)
        let minutes = totalSeconds / 60
        let seconds = totalSeconds % 60
        if minutes > 0 {
            return "\(minutes):\(String(format: "%02d", seconds))"
        }
        return "0:\(String(format: "%02d", seconds))"
    }
}

import SwiftUI

/// One row per negative in the capture queue, showing status, progress, and
/// elapsed time. Replaces the old tile-based strip.
struct CaptureQueueList: View {
    static let maxVisibleRows = 5

    let stitchQueue: StitchQueueModel
    /// The roll being captured; the queue holds other rolls' entries too.
    let rollURL: URL?
    /// Whether a row may be re-scanned now (TETHER_PLAN §4.5), and what to
    /// do when it is. Nil hides the action.
    var canRescan: (StitchQueueModel.QueuedNegative) -> Bool = { _ in false }
    var onRescan: ((StitchQueueModel.QueuedNegative) -> Void)?

    var body: some View {
        // Rows are a fixed height; the list scrolls past `maxVisibleRows`
        // rather than squeezing rows into a shorter frame, where they overlap.
        ScrollView(.vertical) {
            LazyVStack(spacing: 0) {
                ForEach(negatives) { negative in
                    CaptureQueueRow(
                        negative: negative,
                        progress: stitchQueue.progress[negative.id],
                        rollURL: rollURL,
                        isBeingReplaced: isBeingReplaced(negative),
                        canRescan: onRescan != nil && canRescan(negative),
                        onRescan: { onRescan?(negative) }
                    )
                }
            }
        }
        .frame(
            height: CGFloat(min(negatives.count, Self.maxVisibleRows)) * CaptureQueueRow.rowHeight
        )
    }

    /// A later row is a re-scan of this one and has not finished yet.
    private func isBeingReplaced(_ negative: StitchQueueModel.QueuedNegative) -> Bool {
        negative.step != .replaced
            && stitchQueue.hasPendingReplacement(
                rollNegativeID: negative.negativeID, entryID: negative.id
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
    var isBeingReplaced = false
    var canRescan = false
    var onRescan: () -> Void = {}

    @State private var thumbnail: Thumbnail?

    var body: some View {
        HStack(spacing: 8) {
            leadingIcon
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
            HStack(spacing: 3) {
                if negative.replacement != nil {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .foregroundStyle(.secondary)
                }
                Text(negative.stamp)
                    .font(.caption.monospacedDigit())
            }
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
            if canRescan, failedRow {
                Button("Re-scan", action: onRescan)
                    .controlSize(.small)
            }
            trailingLabel
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 50, alignment: .trailing)
        }
        .padding(.horizontal, 8)
        .frame(height: Self.rowHeight)
        .opacity(failedRow ? 0.7 : 1.0)
        .help(helpText)
        .contextMenu {
            if canRescan {
                Button("Re-scan This Negative", action: onRescan)
            }
        }
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
        case .preparing, .checking, .composing, .stitching: "gearshape.fill"
        case .prepareFailed, .checkFailed, .stitchFailed: "xmark.octagon.fill"
        case .replaced: "arrow.triangle.2.circlepath"
        case .waitingForDisk: "externaldrive.badge.exclamationmark"
        case .waitingPrepare, .waitingCheck, .waitingStitch, .waitingCommit: "clock.fill"
        }
    }

    private var iconColor: Color {
        switch negative.step {
        case .published: .green
        case .prepareFailed, .checkFailed, .stitchFailed: .red
        case .stitching, .composing, .preparing, .checking: .orange
        case .waitingForDisk: .yellow
        default: .secondary
        }
    }

    // MARK: - Status text

    private var statusLabel: some View {
        Text(Self.statusText(for: negative, progress: progress, isBeingReplaced: isBeingReplaced))
    }

    static func statusText(
        for negative: StitchQueueModel.QueuedNegative,
        progress: StitchQueueModel.StepProgress?,
        isBeingReplaced: Bool = false
    ) -> String {
        let stepName: String
        if isBeingReplaced { return "Being replaced by a re-scan" }
        switch negative.step {
        case .waitingPrepare: return "Waiting to prepare"
        case .preparing:
            stepName = "Preparing"
        case .waitingCheck: return "Waiting to check"
        case .checking:
            stepName = "Checking"
        case .waitingStitch: return "Waiting to stitch"
        case .composing:
            stepName = "Compositing"
        case .waitingCommit: return "Waiting to publish"
        case .stitching:
            stepName = "Publishing"
        case .published:
            if let replacement = negative.replacement {
                return "Published in place of \(replacement.displayName)"
            }
            return "Published"
        case .replaced: return "Replaced by a re-scan"
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
        case .preparing, .checking, .composing, .stitching: true
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
        case .preparing, .checking, .composing, .stitching:
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

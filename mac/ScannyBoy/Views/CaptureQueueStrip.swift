import SwiftUI

/// One tile per negative captured this session, showing queue progress.
struct CaptureQueueStrip: View {
    let negatives: [StitchQueueModel.QueuedNegative]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(negatives) { negative in
                    CaptureQueueTile(negative: negative)
                }
            }
            .padding(.horizontal, 8)
        }
        .frame(height: 72)
    }
}

private struct CaptureQueueTile: View {
    let negative: StitchQueueModel.QueuedNegative

    var body: some View {
        VStack(spacing: 4) {
            ZStack {
                RoundedRectangle(cornerRadius: 6)
                    .fill(tileColor.opacity(0.15))
                    .frame(width: 64, height: 48)
                Image(systemName: iconName)
                    .foregroundStyle(tileColor)
            }
            Text(negative.stamp)
                .font(.caption2)
                .lineLimit(1)
                .frame(width: 72)
        }
        .help(negative.failureMessage ?? negative.step.rawValue)
    }

    private var tileColor: Color {
        switch negative.step {
        case .published: .green
        case .prepareFailed, .checkFailed, .stitchFailed: .red
        case .stitching, .preparing, .checking: .orange
        case .waitingForDisk: .yellow
        default: .secondary
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
}

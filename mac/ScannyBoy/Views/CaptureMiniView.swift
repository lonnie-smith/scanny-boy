import SwiftUI

/// Grid mini-view: one cell per scan in the negative.
struct CaptureMiniView: View {
    let across: Int
    let down: Int
    let cellStates: [CaptureCellState]
    let cellWarnings: [Int: [String]]

    var body: some View {
        let columns = Array(repeating: GridItem(.flexible(), spacing: 4), count: max(across, 1))
        LazyVGrid(columns: columns, spacing: 4) {
            ForEach(Array(cellStates.enumerated()), id: \.offset) { index, state in
                CaptureCellView(
                    index: index,
                    state: state,
                    warnings: cellWarnings[index] ?? []
                )
            }
        }
        .padding(8)
    }
}

private struct CaptureCellView: View {
    let index: Int
    let state: CaptureCellState
    let warnings: [String]
    @State private var thumbnail: Thumbnail?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 6)
                .fill(backgroundColor)
                .aspectRatio(3 / 2, contentMode: .fit)
            if let thumbnail {
                Image(nsImage: thumbnail.image)
                    .resizable()
                    .scaledToFill()
                    .clipShape(RoundedRectangle(cornerRadius: 6))
            } else {
                Text(label)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .overlay(alignment: .topTrailing) {
            if !warnings.isEmpty {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .padding(4)
                    .help(warnings.joined(separator: ", "))
            }
        }
        .overlay {
            RoundedRectangle(cornerRadius: 6)
                .strokeBorder(borderColor, lineWidth: state == .next ? 2 : 1)
        }
        .task(id: fileURL) {
            guard let fileURL else { return }
            thumbnail = await ThumbnailLoader.shared.thumbnail(
                for: fileURL,
                pointSize: CGSize(width: 120, height: 80),
                scale: 2
            )
        }
    }

    private var fileURL: URL? {
        if case .filled(let url) = state { return url }
        return nil
    }

    private var label: String {
        switch state {
        case .empty: ""
        case .next: "next"
        case .exposing: "exposing"
        case .downloading: "…"
        case .filled: ""
        case .failed: "failed"
        }
    }

    private var backgroundColor: Color {
        switch state {
        case .failed: .red.opacity(0.15)
        case .next: .accentColor.opacity(0.12)
        default: Color.secondary.opacity(0.08)
        }
    }

    private var borderColor: Color {
        switch state {
        case .next: .accentColor
        case .failed: .red
        default: .secondary.opacity(0.35)
        }
    }
}

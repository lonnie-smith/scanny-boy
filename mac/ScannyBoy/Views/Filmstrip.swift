import AppKit
import SwiftUI

/// The filmstrip: every negative as a small clickable cell in
/// `visibleNegatives` order. Shared by the Edit tab (rotation/flip/delete)
/// and the Metadata tab (per-image metadata) — both show the same
/// roll-with-selection browser, so both drive it through the same
/// `EditModel` selection methods and the same click-modifier convention:
/// a plain click selects one frame, shift extends a range, command
/// toggles.
struct FilmstripView: View {
    let negatives: [RollManifest.Negative]
    let cameraColor: RollManifest.CameraColor?
    let isSelected: (String) -> Bool
    let onSelect: (String, _ additive: Bool, _ extendingRange: Bool) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .center, spacing: 8) {
                ForEach(negatives, id: \.negativeID) { negative in
                    FilmstripCell(
                        negative: negative,
                        cameraColor: cameraColor,
                        isSelected: isSelected(negative.negativeID)
                    ) { additive, extendingRange in
                        onSelect(negative.negativeID, additive, extendingRange)
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .frame(height: 104)
    }
}

/// One cell of the filmstrip: a small thumbnail, highlighted when selected,
/// clickable to select its negative. Clicks carry their modifier flags to
/// the model: shift extends a range, command toggles a frame.
struct FilmstripCell: View {
    let negative: RollManifest.Negative
    let cameraColor: RollManifest.CameraColor?
    let isSelected: Bool
    let onSelect: (_ additive: Bool, _ extendingRange: Bool) -> Void

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?

    private static let cellSize = CGSize(width: 80, height: 80)

    var body: some View {
        Button {
            // NSEvent.pressedMouseButtons style probing is not needed here:
            // the click's own modifier flags are on the current event.
            let flags = NSApp.currentEvent?.modifierFlags ?? []
            onSelect(
                flags.contains(.command),
                flags.contains(.shift)
            )
        } label: {
            preview
                .frame(width: Self.cellSize.width, height: Self.cellSize.height)
                .background(isSelected ? Color.accentColor.opacity(0.18) : .clear)
                .overlay {
                    RoundedRectangle(cornerRadius: 5)
                        .strokeBorder(
                            isSelected ? Color.accentColor : Color.clear,
                            lineWidth: 2
                        )
                }
                .clipShape(RoundedRectangle(cornerRadius: 5))
        }
        .buttonStyle(.plain)
        .help(negative.expectedOutput)
        .task(id: "\(negative.previewPath ?? "none")#\(EditModel.renderGeneration(of: negative, cameraColor: cameraColor))") {
            thumbnail = nil
            guard let previewPath = negative.previewPath else {
                return
            }
            thumbnail = await ThumbnailLoader.shared.thumbnail(
                forPreview: URL(filePath: previewPath),
                generation: EditModel.renderGeneration(of: negative, cameraColor: cameraColor),
                pointSize: Self.cellSize,
                scale: displayScale
            )
        }
    }

    @ViewBuilder
    private var preview: some View {
        if let thumbnail {
            Image(nsImage: thumbnail.image)
                .resizable()
                .interpolation(.medium)
                .aspectRatio(contentMode: .fit)
        } else if negative.isCompleted {
            RoundedRectangle(cornerRadius: 5)
                .fill(.quaternary)
                .overlay {
                    Image(systemName: "photo")
                        .foregroundStyle(.secondary)
                }
        } else {
            RoundedRectangle(cornerRadius: 5)
                .fill(.quaternary)
                .overlay {
                    Text(negative.status)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
        }
    }
}

/// What to show when a large preview has no image yet.
enum PreviewPlaceholderKind {
    case loading
    case empty
    case status(String)
}

/// Gray placeholder for a large preview pane — loading spinner, empty
/// photo icon, or status text for an unconverted negative.
struct PreviewPlaceholder: View {
    let kind: PreviewPlaceholderKind
    var cornerRadius: CGFloat = 6

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius)
            .fill(.quaternary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .overlay {
                switch kind {
                case .loading:
                    ProgressView()
                case .empty:
                    Image(systemName: "photo")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                case .status(let status):
                    VStack(spacing: 6) {
                        Image(systemName: "photo")
                            .font(.largeTitle)
                            .foregroundStyle(.secondary)
                        Text("Status: \(status)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
    }
}

/// The large preview of the anchor negative — the image itself plus its
/// generation-keyed thumbnail loading. The panes around it (the Edit tab's
/// rotate/flip/delete strip, the Metadata tab's field form) belong to their
/// own views; this is the shared middle.
struct PreviewImageView: View {
    let negative: RollManifest.Negative
    let cameraColor: RollManifest.CameraColor?

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?
    @State private var isLoadingPreview = false

    var body: some View {
        preview
            .task(id: previewIdentity) {
                thumbnail = nil
                guard previewURL != nil else { return }
                isLoadingPreview = true
                defer { isLoadingPreview = false }
                guard let url = previewURL else { return }
                thumbnail = await ThumbnailLoader.shared.thumbnail(
                    forPreview: url,
                    generation: previewGeneration,
                    pointSize: CGSize(width: 1200, height: 1200),
                    scale: displayScale
                )
            }
    }

    /// Path plus net transform: the CLI rewrites the preview file in
    /// place, so the pair is what tells the thumbnail cache the contents
    /// changed.
    private var previewIdentity: String {
        "\(negative.previewPath ?? "none")#\(previewGeneration)"
    }

    private var previewGeneration: String {
        EditModel.renderGeneration(of: negative, cameraColor: cameraColor)
    }

    private var previewURL: URL? {
        guard let previewPath = negative.previewPath else { return nil }
        return URL(filePath: previewPath)
    }

    @ViewBuilder
    private var preview: some View {
        if let thumbnail {
            Image(nsImage: thumbnail.image)
                .resizable()
                .interpolation(.medium)
                .aspectRatio(contentMode: .fit)
        } else if isLoadingPreview && negative.isCompleted {
            PreviewPlaceholder(kind: .loading)
        } else if negative.isCompleted {
            PreviewPlaceholder(kind: .empty)
        } else {
            PreviewPlaceholder(kind: .status(negative.status))
        }
    }
}

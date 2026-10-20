import SwiftUI

/// Selects the area of clear film base on a base frame whose rebate is only
/// a strip — an ordinary negative's edge rather than a frame of leader. The
/// operator drags a rectangle over the NEF's embedded preview; Measure hands
/// it on as a `BaseFrameRegion` for `roll set-base-frame --region`.
///
/// The preview is ImageIO's, with the camera's orientation applied, and the
/// CLI's decode applies the same orientation, so a fraction of one is the
/// same piece of film in the other.
struct BaseFrameRegionSheet: View {
    let frameURL: URL
    let onMeasure: (BaseFrameRegion) -> Void

    @Environment(\.dismiss) private var dismiss
    @Environment(\.displayScale) private var displayScale
    @State private var preview: Thumbnail?
    @State private var hasFinishedLoading = false
    /// The drawn rectangle in fractions of the preview, origin top-left.
    @State private var selection: CGRect?

    private static let previewSize = CGSize(width: 1200, height: 900)

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Select Clear Film Base")
                .font(.headline)
            Text(Self.instructions)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            previewArea
                .frame(minWidth: 560, idealWidth: 720, minHeight: 380, idealHeight: 480)
            HStack {
                Text(frameURL.lastPathComponent)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Measure") {
                    guard let region = selectedRegion else { return }
                    onMeasure(region)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(selectedRegion == nil)
            }
        }
        .padding(20)
        .task(id: frameURL) {
            let url = frameURL
            let size = Self.previewSize
            let scale = displayScale
            preview = await Task.detached {
                ThumbnailLoader.embeddedPreview(url: url, pointSize: size, scale: scale)
            }.value
            hasFinishedLoading = true
        }
    }

    private var selectedRegion: BaseFrameRegion? {
        selection.flatMap(BaseFrameRegion.init(normalizedRect:))
    }

    @ViewBuilder
    private var previewArea: some View {
        if let preview {
            Image(nsImage: preview.image)
                .resizable()
                .aspectRatio(contentMode: .fit)
                // `.fit` sizes the view to the picture itself, so the
                // overlay's coordinates are the picture's.
                .overlay {
                    GeometryReader { proxy in
                        selectionOverlay(in: proxy.size)
                            .contentShape(Rectangle())
                            .gesture(drawGesture(in: proxy.size))
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if hasFinishedLoading {
            ContentUnavailableView(
                "No Preview",
                systemImage: "photo",
                description: Text("This file has no embedded preview to select an area on.")
            )
        } else {
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func selectionOverlay(in size: CGSize) -> some View {
        let rect = selection.map { Self.denormalized($0, in: size) } ?? .zero
        return ZStack {
            Color.clear
            if selection != nil {
                Path { $0.addRect(rect) }
                    .fill(Color.accentColor.opacity(0.2))
                Path { $0.addRect(rect) }
                    .stroke(Color.accentColor, lineWidth: 2)
            }
        }
    }

    private func drawGesture(in size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 2)
            .onChanged { value in
                selection = Self.normalizedRect(
                    from: value.startLocation, to: value.location, in: size
                )
            }
    }

    /// The rectangle spanned by a drag, in fractions of `size` and clamped
    /// to it, so a drag that leaves the picture stops at its edge.
    nonisolated static func normalizedRect(from start: CGPoint, to end: CGPoint, in size: CGSize) -> CGRect? {
        guard size.width > 0, size.height > 0 else { return nil }
        func clamp(_ value: CGFloat) -> CGFloat { min(max(value, 0), 1) }
        let x0 = clamp(min(start.x, end.x) / size.width)
        let x1 = clamp(max(start.x, end.x) / size.width)
        let y0 = clamp(min(start.y, end.y) / size.height)
        let y1 = clamp(max(start.y, end.y) / size.height)
        guard x1 > x0, y1 > y0 else { return nil }
        return CGRect(x: x0, y: y0, width: x1 - x0, height: y1 - y0)
    }

    private static func denormalized(_ rect: CGRect, in size: CGSize) -> CGRect {
        CGRect(
            x: rect.minX * size.width,
            y: rect.minY * size.height,
            width: rect.width * size.width,
            height: rect.height * size.height
        )
    }

    private static let instructions = """
        Drag a rectangle over clear film base only — the rebate along an edge \
        or between frames. Leave out bare light, sprocket holes and the \
        picture. The rebate must fill most of the rectangle.
        """
}

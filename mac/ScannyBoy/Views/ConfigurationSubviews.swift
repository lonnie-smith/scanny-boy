import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Drag/drop helpers for catalogue rows. In-app Convert drops carry a JSON
/// list of catalogue filenames — not file URLs.
enum CatalogueDragSupport {
    /// The filenames one row should export: its multi-selection when it is
    /// selected, otherwise just itself.
    static func filenamesForDrag(name: String, selectedFiles: Set<String>) -> [String] {
        selectedFiles.contains(name) ? Array(selectedFiles) : [name]
    }

    static func encodeDragPayload(_ filenames: [String]) -> String? {
        guard let data = try? JSONEncoder().encode(filenames) else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    static func decodeDragPayload(_ string: String) -> [String]? {
        guard let data = string.data(using: .utf8),
            let names = try? JSONDecoder().decode([String].self, from: data),
            !names.isEmpty
        else { return nil }
        return names
    }

    static func loadFileURL(from provider: NSItemProvider) async -> URL? {
        await withCheckedContinuation { continuation in
            if provider.canLoadObject(ofClass: URL.self) {
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    continuation.resume(returning: url)
                }
            } else if provider.canLoadObject(ofClass: NSURL.self) {
                _ = provider.loadObject(ofClass: NSURL.self) { nsurl, _ in
                    continuation.resume(returning: (nsurl as? NSURL) as URL?)
                }
            } else {
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                    if let url = item as? URL {
                        continuation.resume(returning: url)
                    } else if let nsurl = item as? NSURL {
                        continuation.resume(returning: nsurl as URL)
                    } else {
                        continuation.resume(returning: nil)
                    }
                }
            }
        }
    }
}

/// One `warning` or `error` event rendered as a labelled, coloured line.
struct IssueLabel: View {
    enum Style {
        case warning
        case error
    }

    let issue: ConfigurationModel.Issue
    let style: Style

    var body: some View {
        Label(issue.message, systemImage: style == .error ? "xmark.octagon" : "exclamationmark.triangle")
            .font(.caption)
            .foregroundStyle(style == .error ? .red : .orange)
    }
}

/// `probe_result`'s `groups`: the selection chunked into negatives, in
/// canonical order, exactly as the CLI computed it.
struct GroupingPreview: View {
    let groups: [[String]]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(groups.enumerated()), id: \.offset) { index, members in
                Text("Negative \(index + 1): \(members.joined(separator: ", "))")
                    .font(.caption)
            }
        }
    }
}

/// One catalogue row: a preview of the RAW file beside its filename.
///
/// The picture is the point — these are film negatives whose filenames are
/// interchangeable, so `_DSC4638.NEF` alone says nothing about which frame it
/// is. Loading is per row and driven by `.task`, so only the rows
/// the `List` is actually showing ever ask for one, and `ThumbnailLoader`
/// caches the answers.
struct CatalogueRow: View {
    /// Big enough to tell two frames of one negative apart in the catalogue
    /// column's default width, small enough that the list still scrolls like
    /// a list.
    static let thumbnailSize = CGSize(width: 80, height: 80)

    let name: String
    /// `nil` only in the moment between the folder changing and the new
    /// catalogue arriving.
    let url: URL?
    let selectedFiles: Set<String>

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?
    @State private var hasFinishedLoading = false

    var body: some View {
        Group {
            if let dragPayload {
                draggableRow
                    .draggable(dragPayload) {
                        CatalogueDragPreview(image: thumbnail?.image, count: dragCount)
                    }
            } else {
                draggableRow
            }
        }
        .task(id: url) {
            guard let url else { return }
            thumbnail = await ThumbnailLoader.shared.thumbnail(
                for: url,
                pointSize: Self.thumbnailSize,
                scale: displayScale
            )
            hasFinishedLoading = true
        }
    }

    private var draggableRow: some View {
        rowContent
            // SwiftUI caches drag previews; tie the id to the selection being
            // dragged so a multi-select badge updates before the gesture.
            .id(dragPreviewIdentity)
    }

    private var dragPayload: String? {
        guard url != nil else { return nil }
        let filenames = CatalogueDragSupport.filenamesForDrag(
            name: name,
            selectedFiles: selectedFiles
        )
        return CatalogueDragSupport.encodeDragPayload(filenames)
    }

    private var dragCount: Int {
        CatalogueDragSupport.filenamesForDrag(name: name, selectedFiles: selectedFiles).count
    }

    private var dragPreviewIdentity: String {
        let filenames = CatalogueDragSupport.filenamesForDrag(
            name: name,
            selectedFiles: selectedFiles
        )
        return "\(name)-\(filenames.sorted().joined(separator: "|"))"
    }

    private var rowContent: some View {
        HStack(spacing: 10) {
            preview
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
                .accessibilityHidden(true)
            Text(name)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
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
                    // Nothing at all while it loads: a spinner per row turns
                    // scrolling a folder into a wall of movement. The icon
                    // appears only once this file is known to have no
                    // preview.
                    if hasFinishedLoading {
                        Image(systemName: "photo")
                            .foregroundStyle(.secondary)
                    }
                }
        }
    }
}

/// Compact drag image for catalogue rows: one thumbnail and, when several
/// scans are selected, the count beside it — not a stack of every row.
struct CatalogueDragPreview: View {
    static let thumbnailSize = CGSize(width: 64, height: 64)

    let image: NSImage?
    let count: Int

    var body: some View {
        HStack(spacing: 8) {
            previewImage
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
            if count > 1 {
                Text("\(count)")
                    .font(.title2.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
            }
        }
        .padding(10)
        .background {
            RoundedRectangle(cornerRadius: 8)
                .fill(.background)
                .shadow(color: .black.opacity(0.25), radius: 6, y: 3)
        }
    }

    @ViewBuilder
    private var previewImage: some View {
        if let image {
            Image(nsImage: image)
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

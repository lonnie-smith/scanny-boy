import SwiftUI

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
/// is (punchlist: "I need to be able to see image previews, not just
/// filenames"). Loading is per row and driven by `.task`, so only the rows
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

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?
    @State private var hasFinishedLoading = false

    var body: some View {
        HStack(spacing: 10) {
            preview
                .frame(width: Self.thumbnailSize.width, height: Self.thumbnailSize.height)
                .accessibilityHidden(true)
            Text(name)
                .lineLimit(1)
                .truncationMode(.middle)
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

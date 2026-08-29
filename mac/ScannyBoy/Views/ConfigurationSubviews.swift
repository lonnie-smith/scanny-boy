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

/// Section 3.9's disk estimate, alongside free space on the output volume at
/// probe time.
struct DiskEstimateView: View {
    let requiredBytes: Int
    let availableBytes: Int

    private static let formatter: ByteCountFormatter = {
        let formatter = ByteCountFormatter()
        formatter.countStyle = .file
        return formatter
    }()

    var body: some View {
        let required = Self.formatter.string(fromByteCount: Int64(requiredBytes))
        let available = Self.formatter.string(fromByteCount: Int64(availableBytes))
        Text("Needs about \(required); \(available) free on the output volume")
            .font(.caption)
            .foregroundStyle(requiredBytes > availableBytes ? .red : .secondary)
    }
}

/// Section 3.6: "show the exact files that will be replaced and require
/// confirmation" before a matching rerun overwrites them.
struct OverwritePreview: View {
    let conflicts: [String]
    @Binding var confirmed: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(conflicts.count) file(s) already exist and will be replaced:")
                .font(.caption)
                .foregroundStyle(.orange)
            Text(conflicts.joined(separator: ", "))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(3)
            Toggle("Replace these files", isOn: $confirmed)
        }
    }
}

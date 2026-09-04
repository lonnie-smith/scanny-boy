import AppKit
import SwiftUI

/// Protocol version 5's Edit tab: the selected roll's negatives as a
/// filmstrip along the bottom, a large preview of the selected negative
/// above it, and the rotation controls that record nondestructive edits.
///
/// Roll info and metadata (the dirty count, Apply) moved to the Metadata
/// tab; this tab is about seeing and editing the negatives. The preview is
/// the CLI-rendered file the `roll info` event names — Swift rotates
/// nothing and renders nothing itself (Python owns every decision); a
/// rotation button records an op through `edit rotate` and the CLI rewrites
/// the preview in response.
struct EditStageView: View {
    @Bindable var edit: EditModel
    let run: RunModel
    let activity: AppActivity
    /// Called after a deletion the user confirmed — `ContentView` uses it
    /// to re-scan the library so the sidebar's negative count keeps up.
    var onNegativeDeleted: () -> Void = {}

    var body: some View {
        VStack(spacing: 0) {
            if let negative = edit.selectedNegative {
                PreviewPane(
                    negative: negative,
                    edit: edit,
                    runIsActive: activity.isBusy,
                    onNegativeDeleted: onNegativeDeleted
                )
            } else {
                ContentUnavailableView(
                    "No Negatives Yet",
                    systemImage: "photo.stack",
                    description: Text("Convert scans into this roll to see them here.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Divider()
            filmstrip
        }
        .background {
            selectionShortcuts
        }
        // Nothing about the roll may change while any helper in the app is
        // busy (`AppActivity`) — not just this app's own run, but a
        // conversion, export, or flat-field calibration too. This also
        // disables the (invisible) selection-shortcut buttons above, so
        // Option-arrow cannot move the selection mid-run either.
        .disabled(activity.isBusy)
        // `initial: true` matters: a run usually finishes while this tab is
        // not mounted (runs are started from Add Scans), so the phase can
        // already be `.finished` when the tab first appears — and that is
        // exactly when the pre-run roll state it would otherwise show is
        // stale.
        .onChange(of: run.phase, initial: true) { _, phase in
            if phase == .finished { edit.refresh() }
        }
    }

    /// Option-left / Option-right move the selection, whether or not the
    /// filmstrip has keyboard focus; Cmd-A selects every frame, Cmd-D
    /// deselects them all.
    private var selectionShortcuts: some View {
        Group {
            Button("Previous Negative") { edit.selectPrevious() }
                .keyboardShortcut(.leftArrow, modifiers: .option)
            Button("Next Negative") { edit.selectNext() }
                .keyboardShortcut(.rightArrow, modifiers: .option)
            Button("Select All Negatives") { edit.selectAll() }
                .keyboardShortcut("a", modifiers: .command)
            Button("Deselect All Negatives") { edit.deselectAll() }
                .keyboardShortcut("d", modifiers: .command)
        }
        .allowsHitTesting(false)
        .opacity(0)
        .accessibilityHidden(true)
    }

    private var filmstrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .center, spacing: 8) {
                ForEach(edit.visibleNegatives, id: \.negativeID) { negative in
                    FilmstripCell(
                        negative: negative,
                        isSelected: edit.isSelected(negative.negativeID)
                    ) { additive, extendingRange in
                        edit.select(
                            negative.negativeID,
                            additive: additive,
                            extendingRange: extendingRange
                        )
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
        }
        .frame(height: 104)
    }
}

/// The selected negative: a preview sized to fill the available space, the
    /// rotate/flip controls, and the one-line info strip. The controls act
    /// on the whole multi-selection when one exists — `edit.selectionTargets`
    /// falls back to the anchor frame otherwise.
    private struct PreviewPane: View {
        let negative: RollManifest.Negative
        @Bindable var edit: EditModel
        let runIsActive: Bool
        let onNegativeDeleted: () -> Void

        @Environment(\.displayScale) private var displayScale
        @State private var thumbnail: Thumbnail?
        @State private var isConfirmingDelete = false

        /// The negatives the controls act on, read once per invocation.
        private var targets: [RollManifest.Negative] { edit.selectionTargets }

        var body: some View {
            VStack(spacing: 8) {
                preview
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding([.horizontal, .top], 16)

                HStack(spacing: 12) {
                    Button {
                        Task { await edit.rotate(targets, clockwise: false) }
                    } label: {
                        Image(systemName: "rotate.left")
                    }
                    .disabled(edit.isRotating || edit.isDeleting || runIsActive)
                    .help("Rotate 90° counter-clockwise")
                    .accessibilityLabel("Rotate 90° counter-clockwise")

                    Button {
                        Task { await edit.rotate(targets, clockwise: true) }
                    } label: {
                        Image(systemName: "rotate.right")
                    }
                    .disabled(edit.isRotating || edit.isDeleting || runIsActive)
                    .help("Rotate 90° clockwise")
                    .accessibilityLabel("Rotate 90° clockwise")

                    Button {
                        Task { await edit.flip(targets) }
                    } label: {
                        Image(systemName: "arrow.left.and.right.righttriangle.left.righttriangle.right.fill")
                    }
                    .disabled(edit.isRotating || edit.isDeleting || runIsActive)
                    .help("Flip horizontally")
                    .accessibilityLabel("Flip horizontally")

                    if edit.isRotating {
                        ProgressView()
                            .controlSize(.small)
                    }

                    Text(infoLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Spacer()

                    Button(role: .destructive) {
                        isConfirmingDelete = true
                    } label: {
                        Image(systemName: "trash")
                    }
                    .disabled(edit.isRotating || edit.isDeleting || runIsActive)
                    .help(deleteButtonHelp)
                    .accessibilityLabel(deleteButtonHelp)
                }
                .padding([.horizontal, .bottom], 16)
            }
            .confirmationDialog(
                deleteDialogTitle,
                isPresented: $isConfirmingDelete
            ) {
                Button("Delete", role: .destructive) {
                    Task {
                        await edit.delete(targets)
                        onNegativeDeleted()
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(deleteDialogMessage)
            }
            .task(id: previewIdentity) {
                thumbnail = nil
                guard let url = previewURL else {
                    return
                }
                thumbnail = await ThumbnailLoader.shared.thumbnail(
                    forPreview: url,
                    generation: previewGeneration,
                    pointSize: CGSize(width: 1200, height: 1200),
                    scale: displayScale
                )
            }
        }

        private var deleteButtonHelp: String {
            targets.count > 1 ? "Delete \(targets.count) Negatives…" : "Delete Negative…"
        }

        private var deleteDialogTitle: String {
            targets.count == 1
                ? "Delete “\(negative.expectedOutput)”?"
                : "Delete \(targets.count) Negatives?"
        }

        private var deleteDialogMessage: String {
            targets.count == 1
                ? """
                Its published TIFF is removed from the roll folder and its \
                record is removed from the library. This cannot be undone.
                """
                : """
                Their published TIFFs are removed from the roll folder and \
                their records are removed from the library. This cannot be \
                undone.
                """
        }

        private var infoLine: String {
            var parts = [negative.expectedOutput]
            if let rms = negative.globalRMSPixels {
                parts.append(String(format: "Global RMS: %.3f px", rms))
            }
            let selectionCount = edit.selectionTargets.count
            if selectionCount > 1 {
                parts.append("\(selectionCount) selected")
            }
            return parts.joined(separator: "  ·  ")
        }

        /// Path plus net transform: the CLI rewrites the preview file in
        /// place, so the pair is what tells the thumbnail cache the
        /// contents changed.
        private var previewIdentity: String {
            "\(negative.previewPath ?? "none")#\(previewGeneration)"
        }

        private var previewGeneration: String {
            "\(negative.rotationQuarterTurns)#\(negative.flippedHorizontally)"
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
        } else if negative.isCompleted {
            RoundedRectangle(cornerRadius: 6)
                .fill(.quaternary)
                .overlay {
                    Image(systemName: "photo")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                }
        } else {
            RoundedRectangle(cornerRadius: 6)
                .fill(.quaternary)
                .overlay {
                    VStack(spacing: 6) {
                        Image(systemName: "photo")
                            .font(.largeTitle)
                            .foregroundStyle(.secondary)
                        Text("Status: \(negative.status)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
        }
    }
}

/// One cell of the filmstrip: a small thumbnail, highlighted when selected,
/// clickable to select its negative. Clicks carry their modifier flags to
/// the model: shift extends a range, command toggles a frame.
private struct FilmstripCell: View {
    let negative: RollManifest.Negative
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
        .task(id: "\(negative.previewPath ?? "none")#\(negative.rotationQuarterTurns)#\(negative.flippedHorizontally)") {
            thumbnail = nil
            guard let previewPath = negative.previewPath else {
                return
            }
            thumbnail = await ThumbnailLoader.shared.thumbnail(
                forPreview: URL(filePath: previewPath),
                generation: "\(negative.rotationQuarterTurns)#\(negative.flippedHorizontally)",
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

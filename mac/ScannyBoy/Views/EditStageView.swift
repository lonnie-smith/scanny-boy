import AppKit
import SwiftUI

/// Protocol version 5's Edit tab: the selected roll's negatives as a
/// filmstrip along the bottom, a large preview of the selected negative
/// above it, and the rotation controls that record nondestructive edits.
///
/// Roll info and metadata (name, capture date, the extended fields, the
/// per-image browser) live on the Metadata tab; this tab is about seeing
/// and transforming the negatives. The preview is the CLI-rendered file
/// the `roll info` event names — Swift rotates nothing and renders nothing
/// itself (Python owns every decision); a rotation button records an op
/// through `edit rotate` and the CLI rewrites the preview in response.
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
            FilmstripView(
                negatives: edit.visibleNegatives,
                isSelected: edit.isSelected
            ) { negativeID, additive, extendingRange in
                edit.select(
                    negativeID,
                    additive: additive,
                    extendingRange: extendingRange
                )
            }
        }
        .background {
            SelectionShortcutButtons(
                onPrevious: edit.selectPrevious,
                onNext: edit.selectNext,
                onSelectAll: edit.selectAll,
                onDeselectAll: edit.deselectAll
            )
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
}

/// The selected negative: a preview sized to fill the available space (or,
/// after a space+click, a 1:1 crop of it), the rotate/flip controls, and
/// the one-line info strip. The controls act on the whole multi-selection
/// when one exists — `edit.selectionTargets` falls back to the anchor
/// frame otherwise.
private struct PreviewPane: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    let runIsActive: Bool
    let onNegativeDeleted: () -> Void

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?
    @State private var isConfirmingDelete = false
    @State private var isTonePanelPresented = false
    @State private var zoom = PreviewZoomModel()

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
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive)
                .help("Rotate 90° counter-clockwise")
                .accessibilityLabel("Rotate 90° counter-clockwise")

                Button {
                    Task { await edit.rotate(targets, clockwise: true) }
                } label: {
                    Image(systemName: "rotate.right")
                }
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive)
                .help("Rotate 90° clockwise")
                .accessibilityLabel("Rotate 90° clockwise")

                Button {
                    Task { await edit.flip(targets) }
                } label: {
                    Image(systemName: "arrow.left.and.right.righttriangle.left.righttriangle.right.fill")
                }
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive)
                .help("Flip horizontally")
                .accessibilityLabel("Flip horizontally")

                Button {
                    isTonePanelPresented = true
                } label: {
                    Image(systemName: "slider.horizontal.3")
                }
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive)
                .help("Tone: paper grade and midtone snap (preview only)")
                .accessibilityLabel("Tone adjustment")
                .popover(isPresented: $isTonePanelPresented, arrowEdge: .bottom) {
                    ToneAdjustmentPanel(
                        toneGradeR: negative.toneGradeR,
                        toneSnapGamma: negative.toneSnapGamma,
                        isBusy: edit.isSettingTone || edit.isRotating || edit.isDeleting,
                        onCommit: { grade, snap in
                            Task { await edit.setTone(targets, gradeR: grade, snapGamma: snap) }
                        },
                        onReset: {
                            Task { await edit.setTone(targets, gradeR: nil, snapGamma: nil) }
                        }
                    )
                    .frame(width: 280)
                }

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
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive)
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
            zoom.reset()
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
        .onChange(of: displayScale) {
            // The 1:1 crop is sized in physical pixels; a moved window (or
            // display change) resizes it.
            zoom.invalidate()
            if zoom.mode == .pixels100 { zoom.fetchCrop() }
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
            parts.append(String(format: "Stitch registration error (RMS): %.1f px", rms))
        }
        // The CLI's fitted rig-tilt rectification: displayed, never
        // recomputed (docs/RECTIFICATION_PLAN.md section 7).
        if let rectification = negative.rectification {
            parts.append(
                String(
                    format: "Rig tilt corrected (%.0f%% fit improvement)",
                    rectification.relativeImprovement * 100
                )
            )
        }
        if let output = negative.output {
            let megapixels = Double(output.width * output.height) / 1_000_000
            parts.append(String(format: "%d × %d (%.1f MP)", output.width, output.height, megapixels))
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
        EditModel.renderGeneration(of: negative)
    }

    private var previewURL: URL? {
        guard let previewPath = negative.previewPath else { return nil }
        return URL(filePath: previewPath)
    }

    @ViewBuilder
    private var preview: some View {
        GeometryReader { geo in
            ZStack {
                if zoom.mode == .pixels100, negative.output != nil {
                    zoomedCrop
                } else if let thumbnail {
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
            .frame(width: geo.size.width, height: geo.size.height)
            .overlay {
                PreviewEventHost(zoom: zoom)
            }
            .onChange(of: geo.size, initial: true) {
                zoom.update(
                    paneSize: geo.size,
                    displayScale: displayScale,
                    displaySize: displaySize,
                    loader: { rect in
                        await edit.renderRegion(negative, rect: rect)
                    }
                )
                if zoom.mode == .pixels100 { zoom.fetchCrop() }
            }
        }
    }

    /// The 1:1 crop the CLI rendered: one image pixel per physical screen
    /// pixel, drawn hard-edged, translated by the live pan.
    @ViewBuilder
    private var zoomedCrop: some View {
        Group {
            if let crop = zoom.crop {
                Image(nsImage: crop.image)
                    .interpolation(.none)
                    .frame(
                        width: CGFloat(crop.rect.width) / crop.displayScale,
                        height: CGFloat(crop.rect.height) / crop.displayScale
                    )
                    .offset(zoom.cropScreenOffset)
                    .background(Color.black)
            } else {
                RoundedRectangle(cornerRadius: 6)
                    .fill(.quaternary)
                    .overlay {
                        ProgressView()
                    }
            }
        }
        .clipped()
    }

    /// The current image's display-space size, when it has been stitched.
    private var displaySize: CGSize {
        guard let output = negative.output else { return .zero }
        return PreviewZoomModel.displaySize(
            tiffSize: CGSize(width: output.width, height: output.height),
            quarterTurns: negative.rotationQuarterTurns
        )
    }
}

/// The Edit tab's tone adjustment panel: an ISO-R paper-grade slider
/// (50–180, lower is harder — the vocabulary the Phase 4 print stage will
/// use) plus a midtone-snap slider (−0.5…0.5), both recorded
/// nondestructively through `edit tone`. Sliders commit on release: one
/// CLI round trip per gesture, and repeated commits coalesce into the
/// trailing `tone` op. Reset removes the op entirely, returning to the
/// flat linear preview the unadjusted display encode gives.
private struct ToneAdjustmentPanel: View {
    /// The anchor negative's recorded tone — what the sliders sync to when
    /// the panel opens. `nil` = the flat look.
    let toneGradeR: Double?
    let toneSnapGamma: Double?
    let isBusy: Bool
    let onCommit: (_ gradeR: Double, _ snapGamma: Double) -> Void
    let onReset: () -> Void

    @State private var grade: Double = 115
    @State private var snap: Double = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Paper Grade")
                        .font(.callout)
                    Spacer()
                    Text("R\(Int(grade))")
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Slider(
                    value: $grade,
                    in: 50...180,
                    onEditingChanged: { editing in
                        guard !editing else { return }
                        onCommit(grade, snap)
                    }
                )
                .accessibilityLabel("Paper grade")
                Text("50–180, lower is harder")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Snap")
                        .font(.callout)
                    Spacer()
                    Text(String(format: "%+.2f", snap))
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                Slider(
                    value: $snap,
                    in: -0.5...0.5,
                    onEditingChanged: { editing in
                        guard !editing else { return }
                        onCommit(grade, snap)
                    }
                )
                .accessibilityLabel("Midtone snap")
                Text("Midtone contrast trim")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Divider()

            HStack {
                Button("Reset", action: onReset)
                    .disabled(isBusy)
                    .help("Remove the adjustment and return to the flat linear preview")
                Spacer()
                if isBusy {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
        .padding(16)
        .onAppear {
            if let toneGradeR, let toneSnapGamma {
                grade = toneGradeR
                snap = toneSnapGamma
            }
        }
    }
}

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
                    run: run,
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
            if showsRunLevelWarnings {
                Text(rollLevelWarningCaption)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
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

    private var showsRunLevelWarnings: Bool {
        guard run.phase == .finished,
              !run.runLevelWarnings.isEmpty,
              let rollURL = edit.rollURL,
              let outputFolder = run.outputFolder
        else { return false }
        return rollURL.standardizedFileURL == outputFolder.standardizedFileURL
    }

    private var rollLevelWarningCaption: String {
        NegativeDiagnostics.rollLevelWarningCaption(run.runLevelWarnings)
    }
}

/// The selected negative: a preview sized to fill the available space (or,
/// after space+click, a 1:1 crop of it), the rotate/flip controls, and
/// the one-line info strip. The controls act on the whole multi-selection
/// when one exists — `edit.selectionTargets` falls back to the anchor
/// frame otherwise.
private struct PreviewPane: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    let run: RunModel
    let runIsActive: Bool
    let onNegativeDeleted: () -> Void

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?
    @State private var isConfirmingDelete = false
    @State private var isTonePanelPresented = false
    @State private var isColorPanelPresented = false
    @State private var zoom = PreviewZoomModel()
    @State private var paneSize: CGSize = .zero

    /// The negatives the controls act on, read once per invocation.
    private var targets: [RollManifest.Negative] { edit.selectionTargets }

    private var rotationShortcutsEnabled: Bool {
        !(edit.isRotating || edit.isDeleting || edit.isSettingTone || edit.isSettingColor || runIsActive)
    }

    private var isMonochromeRoll: Bool {
        edit.roll?.filmKind == "monochrome"
    }

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
                .help("Rotate 90° counter-clockwise (⌘[)")
                .accessibilityLabel("Rotate 90° counter-clockwise")

                Button {
                    Task { await edit.rotate(targets, clockwise: true) }
                } label: {
                    Image(systemName: "rotate.right")
                }
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive)
                .help("Rotate 90° clockwise (⌘])")
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
                    zoom.toggle(at: previewCenter)
                } label: {
                    Image(systemName: zoom.mode == .fit ? "plus.magnifyingglass" : "minus.magnifyingglass")
                }
                .disabled(
                    negative.output == nil
                        || edit.isRotating || edit.isDeleting || edit.isSettingTone || runIsActive
                )
                .help(zoomButtonHelp)
                .accessibilityLabel(zoomButtonHelp)

                Button {
                    isTonePanelPresented = true
                } label: {
                    Image(systemName: "slider.horizontal.3")
                }
                .disabled(edit.isRotating || edit.isDeleting || edit.isSettingTone || edit.isSettingColor || runIsActive)
                .help("Tone: print density, paper grade, zone density, toe/shoulder (preview only)")
                .accessibilityLabel("Tone adjustment")
                .popover(isPresented: $isTonePanelPresented, arrowEdge: .bottom) {
                    ToneAdjustmentPanel(
                        adjustment: negative.toneAdjustment,
                        isBusy: edit.isSettingTone || edit.isSettingColor || edit.isRotating || edit.isDeleting,
                        onScheduleCommit: { adjustment in
                            edit.scheduleTone(targets, adjustment: adjustment)
                        },
                        onCommitNow: { adjustment, auto in
                            Task {
                                await edit.commitTone(
                                    targets, adjustment: adjustment, auto: auto
                                )
                            }
                        },
                        onReset: {
                            Task {
                                await edit.commitTone(targets, adjustment: nil)
                            }
                        }
                    )
                    .frame(width: 360)
                }

                Button {
                    isColorPanelPresented = true
                } label: {
                    Image(systemName: "paintpalette")
                }
                .disabled(
                    isMonochromeRoll
                        || edit.isRotating || edit.isDeleting
                        || edit.isSettingTone || edit.isSettingColor || runIsActive
                )
                .help(
                    isMonochromeRoll
                        ? "Colour adjustment is unavailable on a monochrome roll"
                        : "Colour: balance, cast removal, dye separation (preview only)"
                )
                .accessibilityLabel("Colour adjustment")
                .popover(isPresented: $isColorPanelPresented, arrowEdge: .bottom) {
                    ColorAdjustmentPanel(
                        adjustment: negative.colorAdjustment,
                        isBusy: edit.isSettingColor || edit.isRotating || edit.isDeleting,
                        onScheduleCommit: { adjustment in
                            edit.scheduleColor(targets, adjustment: adjustment)
                        },
                        onCommitNow: { adjustment in
                            Task { await edit.commitColor(targets, adjustment: adjustment) }
                        },
                        onReset: {
                            Task { await edit.commitColor(targets, adjustment: nil) }
                        }
                    )
                    .frame(width: 360)
                }

                if edit.isRotating {
                    ProgressView()
                        .controlSize(.small)
                }

                Text(infoLine)
                    .font(.caption)
                    .foregroundStyle(negative.isFailed ? .red : .secondary)
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
        .background {
            RotationShortcutButtons(
                isEnabled: rotationShortcutsEnabled,
                onRotateCounterClockwise: {
                    Task { await edit.rotate(targets, clockwise: false) }
                },
                onRotateClockwise: {
                    Task { await edit.rotate(targets, clockwise: true) }
                }
            )
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
        .onChange(of: previewIdentity) {
            zoom.reset()
            refreshZoomContext(paneSize: paneSize)
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
        .onChange(of: displayScale) {
            // The 1:1 crop is sized in physical pixels; a moved window (or
            // display change) resizes it.
            zoom.invalidate()
            if zoom.mode == .pixels100 { zoom.fetchCrop() }
        }
    }

    private var zoomButtonHelp: String {
        zoom.mode == .fit
            ? "Zoom to 100% (Space+click)"
            : "Zoom to fit (Space+click)"
    }

    /// The centre of the preview pane — where the toolbar zoom button anchors.
    private var previewCenter: CGPoint {
        CGPoint(x: paneSize.width / 2, y: paneSize.height / 2)
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
        let overlay = runOverlay
        return NegativeDiagnostics.infoParts(
            for: negative,
            runOverlay: overlay,
            selectionCount: edit.selectionTargets.count
        ).joined(separator: "  ·  ")
    }

    /// Fresh per-negative diagnostics from the most recent finished run into
    /// this roll, when the session still holds them.
    private var runOverlay: RunModel.NegativeResult? {
        guard run.phase == .finished,
              let rollURL = edit.rollURL,
              let outputFolder = run.outputFolder,
              rollURL.standardizedFileURL == outputFolder.standardizedFileURL
        else { return nil }
        return run.negativeResult(for: negative.negativeID)
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
                paneSize = geo.size
                refreshZoomContext(paneSize: geo.size)
            }
        }
    }

    private func refreshZoomContext(paneSize: CGSize) {
        guard paneSize.width > 0, paneSize.height > 0 else { return }
        zoom.update(
            paneSize: paneSize,
            displayScale: displayScale,
            displaySize: displaySize,
            loader: { rect in
                await edit.renderRegion(negative, rect: rect)
            }
        )
        if zoom.mode == .pixels100 { zoom.fetchCrop() }
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

/// The Edit tab's tone adjustment panel, grouped like NegPy's Exposure panel.
private struct ToneAdjustmentPanel: View {
    let adjustment: ToneAdjustment?
    let isBusy: Bool
    let onScheduleCommit: (_ adjustment: ToneAdjustment) -> Void
    let onCommitNow: (_ adjustment: ToneAdjustment, _ auto: ToneAutoFlags) -> Void
    let onReset: () -> Void

    private static let gradeRange: ClosedRange<Double> = 50...180
    private static let snapRange: ClosedRange<Double> = -0.5...0.5
    private static let densityRange: ClosedRange<Double> = 0...2
    private static let shadowDensityRange: ClosedRange<Double> = -0.9...0.9
    private static let highlightDensityRange: ClosedRange<Double> = -0.5...0.5
    private static let toeRange: ClosedRange<Double> = -1...1
    private static let widthRange: ClosedRange<Double> = 0.1...5

    @State private var values = ToneAdjustment.neutral

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                sectionHeader("Print")
                sliderRow(
                    title: "Print Density",
                    valueLabel: String(format: "%.2f", values.density),
                    value: $values.density,
                    range: Self.densityRange,
                    step: 0.05,
                    resetValue: ToneAdjustment.neutral.density,
                    accessibilityLabel: "Print density",
                    help: "0.0–2.0, higher is denser",
                    autoHelp: "Solve the density from this negative's own metering",
                    autoFlag: .density
                )
                sliderRow(
                    title: "Paper Grade",
                    valueLabel: "R\(Int(values.gradeR))",
                    value: $values.gradeR,
                    range: Self.gradeRange,
                    step: 1,
                    resetValue: ToneAdjustment.neutral.gradeR,
                    reversed: true,
                    accessibilityLabel: "Paper grade",
                    help: "50–180, lower is harder",
                    autoHelp: "Solve the grade from this negative's own metering",
                    autoFlag: .grade
                )
                sliderRow(
                    title: "Snap",
                    valueLabel: String(format: "%+.2f", values.snapGamma),
                    value: $values.snapGamma,
                    range: Self.snapRange,
                    step: 0.05,
                    resetValue: 0,
                    accessibilityLabel: "Midtone snap",
                    help: "Midtone contrast trim"
                )

                sectionHeader("Zones")
                sliderRow(
                    title: "Shadows Density",
                    valueLabel: String(format: "%+.2f", values.shadowDensity),
                    value: $values.shadowDensity,
                    range: Self.shadowDensityRange,
                    step: 0.05,
                    resetValue: 0,
                    accessibilityLabel: "Shadows density",
                    help: "±0.9, positive adds density"
                )
                sliderRow(
                    title: "Highlights Density",
                    valueLabel: String(format: "%+.2f", values.highlightDensity),
                    value: $values.highlightDensity,
                    range: Self.highlightDensityRange,
                    step: 0.05,
                    resetValue: 0,
                    accessibilityLabel: "Highlights density",
                    help: "±0.5, positive adds density"
                )

                DisclosureGroup("Curve") {
                    VStack(alignment: .leading, spacing: 12) {
                        sliderRow(
                            title: "Toe",
                            valueLabel: String(format: "%+.2f", values.toe),
                            value: $values.toe,
                            range: Self.toeRange,
                            step: 0.05,
                            resetValue: 0,
                            accessibilityLabel: "Toe",
                            help: "Shadow roll-off"
                        )
                        sliderRow(
                            title: "Toe Width",
                            valueLabel: String(format: "%.1f", values.toeWidth),
                            value: $values.toeWidth,
                            range: Self.widthRange,
                            step: 0.1,
                            resetValue: ToneAdjustment.neutral.toeWidth,
                            accessibilityLabel: "Toe width",
                            help: "0.1–5.0"
                        )
                        sliderRow(
                            title: "Shoulder",
                            valueLabel: String(format: "%+.2f", values.shoulder),
                            value: $values.shoulder,
                            range: Self.toeRange,
                            step: 0.05,
                            resetValue: 0,
                            accessibilityLabel: "Shoulder",
                            help: "Highlight roll-off"
                        )
                        sliderRow(
                            title: "Shoulder Width",
                            valueLabel: String(format: "%.1f", values.shoulderWidth),
                            value: $values.shoulderWidth,
                            range: Self.widthRange,
                            step: 0.1,
                            resetValue: ToneAdjustment.neutral.shoulderWidth,
                            accessibilityLabel: "Shoulder width",
                            help: "0.1–5.0"
                        )
                    }
                    .padding(.top, 8)
                }

                Divider()

                HStack {
                    Button("Reset") {
                        values = ToneAdjustment.neutral
                        onReset()
                    }
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
        }
        .frame(maxHeight: 520)
        .onAppear { syncFromModel() }
        .onChange(of: adjustment) { syncFromModel() }
    }

    @ViewBuilder
    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.headline)
    }

    @ViewBuilder
    private func sliderRow(
        title: String,
        valueLabel: String,
        value: Binding<Double>,
        range: ClosedRange<Double>,
        step: Double,
        resetValue: Double,
        reversed: Bool = false,
        accessibilityLabel: String,
        help: String,
        autoHelp: String? = nil,
        autoFlag: ToneAutoFlags = []
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                    .font(.callout)
                Spacer()
                if let autoHelp {
                    Button {
                        onCommitNow(snappedValues, autoFlag)
                    } label: {
                        Image(systemName: "wand.and.stars")
                    }
                    .buttonStyle(.borderless)
                    .disabled(isBusy)
                    .help(autoHelp)
                }
                Text(valueLabel)
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ToneSlider(
                value: value,
                range: range,
                step: step,
                resetValue: resetValue,
                reversed: reversed,
                onScheduleCommit: scheduleCommit,
                onCommitNow: { onCommitNow(snappedValues, []) }
            )
            .accessibilityLabel(accessibilityLabel)
            Text(help)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func scheduleCommit() {
        onScheduleCommit(snappedValues)
    }

    private var snappedValues: ToneAdjustment {
        ToneAdjustment(
            gradeR: ToneSlider.snap(values.gradeR, step: 1, range: Self.gradeRange),
            snapGamma: ToneSlider.snap(values.snapGamma, step: 0.05, range: Self.snapRange),
            density: ToneSlider.snap(values.density, step: 0.05, range: Self.densityRange),
            shadowDensity: ToneSlider.snap(
                values.shadowDensity, step: 0.05, range: Self.shadowDensityRange
            ),
            highlightDensity: ToneSlider.snap(
                values.highlightDensity, step: 0.05, range: Self.highlightDensityRange
            ),
            toe: ToneSlider.snap(values.toe, step: 0.05, range: Self.toeRange),
            toeWidth: ToneSlider.snap(values.toeWidth, step: 0.1, range: Self.widthRange),
            shoulder: ToneSlider.snap(values.shoulder, step: 0.05, range: Self.toeRange),
            shoulderWidth: ToneSlider.snap(
                values.shoulderWidth, step: 0.1, range: Self.widthRange
            )
        )
    }

    private func syncFromModel() {
        values = adjustment ?? ToneAdjustment.neutral
    }
}

private struct ToneSlider: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double
    let resetValue: Double
    var reversed: Bool = false
    let onScheduleCommit: () -> Void
    let onCommitNow: () -> Void

    private var sliderValue: Binding<Double> {
        let base = reversed ? reversedBinding : $value
        return Binding(
            get: { base.wrappedValue },
            set: { newValue in
                let snapped = Self.snap(newValue, step: step, range: range)
                guard snapped != base.wrappedValue else { return }
                base.wrappedValue = snapped
                onScheduleCommit()
            }
        )
    }

    private var reversedBinding: Binding<Double> {
        Binding(
            get: { range.upperBound + range.lowerBound - value },
            set: { value = range.upperBound + range.lowerBound - $0 }
        )
    }

    static func snap(_ value: Double, step: Double, range: ClosedRange<Double>) -> Double {
        let stepped = (value / step).rounded() * step
        return min(range.upperBound, max(range.lowerBound, stepped))
    }

    var body: some View {
        Slider(value: sliderValue, in: range, step: step) { editing in
            guard !editing else { return }
            onCommitNow()
        }
        .simultaneousGesture(
            TapGesture(count: 2).onEnded {
                value = resetValue
                onCommitNow()
            }
        )
    }
}

private enum ColorRegion: String, CaseIterable, Identifiable {
    case global, shadows, highlights
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

private struct ColorAdjustmentPanel: View {
    let adjustment: ColorAdjustment?
    let isBusy: Bool
    let onScheduleCommit: (_ adjustment: ColorAdjustment) -> Void
    let onCommitNow: (_ adjustment: ColorAdjustment) -> Void
    let onReset: () -> Void

    @State private var region: ColorRegion = .global
    @State private var values = ColorAdjustment.neutral
    /// Anchor (M, Y) for the active region for the duration of a temperature drag.
    @State private var temperatureAnchor: (magenta: Double, yellow: Double)?
    @State private var temperatureKelvin = ColorTemperature.neutralKelvin

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Balance").font(.headline)
                Picker("Region", selection: $region) {
                    ForEach(ColorRegion.allCases) { item in
                        Text(item.label).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: region) { syncTemperatureReadout() }

                temperatureSlider

                colorSlider("Cyan", value: cyanBinding, range: -1...1, step: 0.02) {
                    String(format: "%+.2f", cyanBinding.wrappedValue)
                }
                colorSlider("Magenta", value: magentaBinding, range: -1...1, step: 0.02) {
                    String(format: "%+.2f", magentaBinding.wrappedValue)
                }
                colorSlider("Yellow", value: yellowBinding, range: -1...1, step: 0.02) {
                    String(format: "%+.2f", yellowBinding.wrappedValue)
                }

                Text("Correction").font(.headline)
                colorSlider("Cast Removal", value: $values.castRemoval, range: 0...1, step: 0.05) {
                    String(format: "%.2f", values.castRemoval)
                }

                Text("Saturation").font(.headline)
                colorSlider("Dye Separation", value: $values.dyeSeparation, range: 0.5...1.5, step: 0.02) {
                    String(format: "%.2f", values.dyeSeparation)
                }
                colorSlider(
                    "Separation Damping",
                    value: $values.separationDamping,
                    range: 0...1,
                    step: 0.05,
                    disabled: values.dyeSeparation == 1
                ) {
                    String(format: "%.2f", values.separationDamping)
                }
                .help("Redistributes dye separation; inactive at neutral separation")

                HStack {
                    Button("Region Reset") { resetRegion() }
                    Spacer()
                    Button("Reset All", role: .destructive) { onReset() }
                }
            }
            .padding()
        }
        .disabled(isBusy)
        .onAppear(perform: syncFromModel)
        .onChange(of: adjustment) { syncFromModel() }
    }

    private var cyanBinding: Binding<Double> {
        switch region {
        case .global: Binding(get: { values.wbCyan }, set: { values.wbCyan = $0 })
        case .shadows: Binding(get: { values.shadowCyan }, set: { values.shadowCyan = $0 })
        case .highlights:
            Binding(get: { values.highlightCyan }, set: { values.highlightCyan = $0 })
        }
    }

    private var magentaBinding: Binding<Double> {
        switch region {
        case .global:
            Binding(
                get: { values.wbMagenta },
                set: {
                    values.wbMagenta = $0
                    if temperatureAnchor == nil { syncTemperatureReadout() }
                }
            )
        case .shadows:
            Binding(
                get: { values.shadowMagenta },
                set: {
                    values.shadowMagenta = $0
                    if temperatureAnchor == nil { syncTemperatureReadout() }
                }
            )
        case .highlights:
            Binding(
                get: { values.highlightMagenta },
                set: {
                    values.highlightMagenta = $0
                    if temperatureAnchor == nil { syncTemperatureReadout() }
                }
            )
        }
    }

    private var yellowBinding: Binding<Double> {
        switch region {
        case .global:
            Binding(
                get: { values.wbYellow },
                set: {
                    values.wbYellow = $0
                    if temperatureAnchor == nil { syncTemperatureReadout() }
                }
            )
        case .shadows:
            Binding(
                get: { values.shadowYellow },
                set: {
                    values.shadowYellow = $0
                    if temperatureAnchor == nil { syncTemperatureReadout() }
                }
            )
        case .highlights:
            Binding(
                get: { values.highlightYellow },
                set: {
                    values.highlightYellow = $0
                    if temperatureAnchor == nil { syncTemperatureReadout() }
                }
            )
        }
    }

    private func colorSlider(
        _ title: String,
        value: Binding<Double>,
        range: ClosedRange<Double>,
        step: Double,
        disabled: Bool = false,
        label: @escaping () -> String
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                Spacer()
                Text(label())
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            ToneSlider(
                value: value,
                range: range,
                step: step,
                resetValue: resetValue(for: title),
                onScheduleCommit: { onScheduleCommit(values) },
                onCommitNow: { onCommitNow(values) }
            )
            .disabled(disabled)
        }
    }

    private func resetValue(for title: String) -> Double {
        switch title {
        case "Cast Removal", "Separation Damping": 0
        case "Dye Separation": 1
        default: 0
        }
    }

    private func resetRegion() {
        switch region {
        case .global:
            values.wbCyan = 0; values.wbMagenta = 0; values.wbYellow = 0
        case .shadows:
            values.shadowCyan = 0; values.shadowMagenta = 0; values.shadowYellow = 0
        case .highlights:
            values.highlightCyan = 0; values.highlightMagenta = 0; values.highlightYellow = 0
        }
        onCommitNow(values)
    }

    private func syncFromModel() {
        values = adjustment ?? ColorAdjustment.neutral
        syncTemperatureReadout()
        temperatureAnchor = nil
    }

    private func syncTemperatureReadout() {
        let pair = regionMagentaYellow
        temperatureKelvin = ColorTemperature.kelvin(
            magenta: pair.magenta, yellow: pair.yellow
        )
    }

    private var regionMagentaYellow: (magenta: Double, yellow: Double) {
        switch region {
        case .global: (values.wbMagenta, values.wbYellow)
        case .shadows: (values.shadowMagenta, values.shadowYellow)
        case .highlights: (values.highlightMagenta, values.highlightYellow)
        }
    }

    private var temperatureSlider: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Temperature")
                Spacer()
                Text(String(format: "%.0fK", temperatureKelvin))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            Slider(
                value: Binding(
                    get: {
                        ColorTemperature.maxKelvin + ColorTemperature.minKelvin - temperatureKelvin
                    },
                    set: { reversed in
                        let kelvin = ColorTemperature.maxKelvin + ColorTemperature.minKelvin - reversed
                        let snapped = ToneSlider.snap(
                            kelvin, step: 50,
                            range: ColorTemperature.minKelvin...ColorTemperature.maxKelvin
                        )
                        guard snapped != temperatureKelvin else { return }
                        temperatureKelvin = snapped
                        applyTemperature(snapped)
                        onScheduleCommit(values)
                    }
                ),
                in: ColorTemperature.minKelvin...ColorTemperature.maxKelvin,
                step: 50
            ) { editing in
                if editing {
                    let pair = regionMagentaYellow
                    temperatureAnchor = (pair.magenta, pair.yellow)
                } else {
                    temperatureAnchor = nil
                    onCommitNow(values)
                }
            }
        }
    }

    private func applyTemperature(_ kelvin: Double) {
        let anchor = temperatureAnchor ?? regionMagentaYellow
        let balanced = ColorTemperature.whiteBalance(
            kelvin: kelvin,
            anchorMagenta: anchor.magenta,
            anchorYellow: anchor.yellow
        )
        switch region {
        case .global:
            values.wbMagenta = balanced.magenta
            values.wbYellow = balanced.yellow
        case .shadows:
            values.shadowMagenta = balanced.magenta
            values.shadowYellow = balanced.yellow
        case .highlights:
            values.highlightMagenta = balanced.magenta
            values.highlightYellow = balanced.yellow
        }
    }
}

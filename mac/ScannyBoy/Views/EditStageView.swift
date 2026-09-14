import AppKit
import SwiftUI

/// Protocol version 5's Edit tab: the selected roll's negatives as a
/// filmstrip along the bottom, a tabbed adjustment sidebar on the left,
/// a large preview of the selected negative beside it, and a slim toolbar
/// under the preview for zoom and display toggles.
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
    @Bindable var keyboard: AppKeyboardState
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
                    keyboard: keyboard,
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
                cameraColor: edit.roll?.cameraColor,
                highlightLock: edit.roll?.highlightLock,
                isSelected: edit.isSelected,
                warningIDs: warningIDs
            ) { negativeID, additive, extendingRange in
                edit.select(
                    negativeID,
                    additive: additive,
                    extendingRange: extendingRange
                )
            }
            if showsRunLevelWarnings {
                Text("Roll warnings: \(rollLevelWarningCaption)")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
            }
        }
        .onChange(of: edit.selectedNegative?.negativeID) { _, negativeID in
            if negativeID == nil {
                keyboard.previewPaneMounted = false
                keyboard.previewHasOutput = false
            }
        }
        // Nothing about the roll may change while any helper in the app is
        // busy (`AppActivity`) — not just this app's own run, but a
        // conversion, export, or flat-field calibration too.
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

    private var warningIDs: Set<String> {
        var ids = Set<String>()
        for negative in edit.visibleNegatives {
            if NegativeDiagnostics.hasWarnings(for: negative) {
                ids.insert(negative.negativeID)
            }
            if let result = run.negativeResult(for: negative.negativeID),
               !result.warnings.isEmpty
            {
                ids.insert(negative.negativeID)
            }
        }
        return ids
    }
}

/// Sidebar modules on the Edit tab, left to right in the tab picker.
private enum EditSidebarTab: String, CaseIterable, Identifiable {
    case geometry, tone, color, heal

    var id: String { rawValue }

    var label: String {
        switch self {
        case .geometry: "Geometry"
        case .tone: "Tone"
        case .color: "Color"
        case .heal: "Heal"
        }
    }
}

/// The selected negative: a tabbed sidebar of adjustment controls, a
/// preview sized to fill the remaining space (or, after double-click or ⌘Z,
/// a 1:1 crop of it), and a slim toolbar for zoom and display toggles. The
/// controls act on the whole multi-selection when one exists —
/// `edit.selectionTargets` falls back to the anchor frame otherwise.
private struct PreviewPane: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    let run: RunModel
    let runIsActive: Bool
    @Bindable var keyboard: AppKeyboardState
    let onNegativeDeleted: () -> Void

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?
    @State private var isLoadingPreview = false
    @State private var isConfirmingDelete = false
    /// The sensitivity the Heal panel offers. Not the model's state: it
    /// seeds from the negative's recorded sensitivity and is what the
    /// next detect run sends.
    @State private var spotsSensitivity: Double = 0.5
    @State private var zoom = PreviewZoomModel()
    @State private var paneSize: CGSize = .zero
    /// Sticky across negative changes — the point is comparing densities
    /// from frame to frame.
    @State private var showsNegative = false
    /// Sticky across negative changes, like `showsNegative`.
    @State private var selectedTab: EditSidebarTab = .geometry
    /// Tracks the last render path so pixel-only reloads keep the old frame
    /// visible until the new one is decoded.
    @State private var lastPreviewModeIdentity: String?
    /// The crop-mode editing session: the overlay's
    /// rect/tilt/preset. Per-preview state; changing negatives ends it.
    @State private var cropSession = CropSession()

    /// The negatives the controls act on, read once per invocation.
    private var targets: [RollManifest.Negative] { edit.selectionTargets }

    /// The display encode the pane's renders should use.
    private var displayMode: PreviewDisplayMode {
        showsNegative ? .negative : .positive
    }

    private var zoomShortcutsEnabled: Bool {
        negative.output != nil
            && !cropSession.isActive
            && !(edit.isRotating || edit.isDeleting || edit.isSettingTone || edit.isSettingColor || edit.isCropping || runIsActive)
    }

    private var isMonochromeRoll: Bool {
        edit.roll?.filmKind == "monochrome"
    }

    var body: some View {
        HStack(spacing: 0) {
            EditSidebar(
                negative: negative,
                edit: edit,
                selectedTab: $selectedTab,
                spotsSensitivity: $spotsSensitivity,
                targets: targets,
                isMonochromeRoll: isMonochromeRoll,
                runIsActive: runIsActive,
                cropSession: cropSession,
                displaySize: displaySize,
                onBeginCrop: { beginCrop() },
                onApplyCrop: { applyCrop() }
            )
            .frame(width: 360)

            Divider()

            VStack(spacing: 8) {
                preview
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding([.horizontal, .top], 16)

                HStack(spacing: 12) {
                    Button {
                        scheduleZoomToggle(at: previewCenter)
                    } label: {
                        Image(systemName: zoom.mode == .fit ? "plus.magnifyingglass" : "minus.magnifyingglass")
                    }
                    .disabled(
                        negative.output == nil
                            || cropSession.isActive
                            || edit.isRotating || edit.isDeleting || edit.isSettingTone
                            || edit.isSettingColor || edit.isCropping || runIsActive
                    )
                    .help(zoomButtonHelp)
                    .accessibilityLabel(zoomButtonHelp)

                    Button {
                        showsNegative.toggle()
                    } label: {
                        Image(systemName: showsNegative
                            ? "circle.lefthalf.filled.inverse"
                            : "circle.lefthalf.filled")
                    }
                    .disabled(
                        negative.output == nil
                            || cropSession.isActive
                            || edit.isRotating || edit.isDeleting || edit.isSettingTone
                            || edit.isSettingColor || edit.isCropping || runIsActive
                    )
                    .help(displayModeButtonHelp)
                    .accessibilityLabel(displayModeButtonHelp)

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
                    .disabled(
                        cropSession.isActive || edit.isRotating || edit.isDeleting
                            || edit.isSettingTone || edit.isCropping || runIsActive
                    )
                    .help(deleteButtonHelp)
                    .accessibilityLabel(deleteButtonHelp)
                }
                .padding([.horizontal, .bottom], 16)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .onAppear(perform: registerKeyboardShortcuts)
        .onDisappear(perform: unregisterKeyboardShortcuts)
        .onChange(of: previewKeyboardSyncToken) {
            syncKeyboardShortcuts()
        }
        .onChange(of: paneSize) {
            keyboard.zoomToggleCenter = previewCenter
        }
        .onChange(of: keyboard.zoomToggleRequest) {
            guard !cropSession.isActive else { return }
            scheduleZoomToggle(at: previewCenter)
        }
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyBeginCrop)) { _ in
            guard keyboard.canCrop else { return }
            guard !AppKeyboard.isTextInputFirstResponder() else { return }
            selectedTab = .geometry
            if !cropSession.isActive {
                beginCrop()
            }
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
        .onChange(of: previewNegativeIdentity) {
            zoom.reset()
            refreshZoomContext(paneSize: paneSize)
        }
        .onChange(of: previewGeneration) {
            // Sidebar edits change pixels, not zoom — stay at 100% and refetch
            // tiles through the updated loader, like the positive/negative flip.
            zoom.invalidate()
            refreshZoomContext(paneSize: paneSize)
        }
        .onChange(of: negative.negativeID) {
            // The crop session belongs to one negative's preview.
            cropSession.end()
        }
        .onChange(of: showsNegative) {
            // The on-screen 1:1 crop is the other mode's pixels until the
            // new render arrives; forget it and refetch through the new
            // loader. The zoom mode itself is kept — both views zoom.
            zoom.invalidate()
            refreshZoomContext(paneSize: paneSize)
        }
        .task(id: displayIdentity) {
            let modeChanged = previewModeIdentity != lastPreviewModeIdentity
            lastPreviewModeIdentity = previewModeIdentity
            if modeChanged { thumbnail = nil }
            let willLoad = cropSession.isActive || showsNegative || previewURL != nil
            guard willLoad else {
                thumbnail = nil
                return
            }
            isLoadingPreview = true
            defer { isLoadingPreview = false }
            let next: Thumbnail?
            if cropSession.isActive {
                next = await edit.renderPreview(
                    negative, mode: displayMode, fullFrame: true
                )
            } else if showsNegative {
                next = await edit.renderPreview(negative, mode: .negative)
            } else if negative.toneAdjustment == nil {
                // Untoned negatives may still have on-disk preview PNGs from
                // the old flat encode — render live through the default print
                // curve until the cached file is regenerated.
                next = await edit.renderPreview(negative, mode: displayMode)
            } else if let url = previewURL {
                next = await ThumbnailLoader.shared.thumbnail(
                    forPreview: url,
                    generation: previewGeneration,
                    pointSize: CGSize(width: 1200, height: 1200),
                    scale: displayScale
                )
            } else {
                next = nil
            }
            thumbnail = next
        }
        .onChange(of: displayScale) {
            // The 1:1 crop is sized in physical pixels; a moved window (or
            // display change) resizes it.
            zoom.invalidate()
            refreshZoomContext(paneSize: paneSize)
        }
    }

    private var zoomButtonHelp: String {
        zoom.mode == .fit
            ? "Double-click to zoom to 100% (⌘Z)"
            : "Double-click to fit (⌘Z)"
    }

    /// Drives `AppKeyboardState` refresh when preview availability changes.
    private var previewKeyboardSyncToken: String {
        "\(negative.output != nil)|\(edit.isRotating)|\(edit.isDeleting)|\(edit.isSettingTone)|\(edit.isSettingColor)|\(edit.isCropping)|\(runIsActive)|\(cropSession.isActive)"
    }

    private func registerKeyboardShortcuts() {
        keyboard.previewPaneMounted = true
        syncKeyboardShortcuts()
    }

    /// Defers zoom toggles to the next run-loop turn so they never land in
    /// the same SwiftUI frame as a layout pass from the fit ↔ 100% swap.
    private func scheduleZoomToggle(at point: CGPoint) {
        Task { @MainActor in
            zoom.toggle(at: point)
        }
    }

    private func scheduleZoomIn(at point: CGPoint) {
        Task { @MainActor in
            zoom.zoomIn(at: point)
        }
    }

    private func scheduleZoomOut() {
        Task { @MainActor in
            zoom.zoomOut()
        }
    }

    private func unregisterKeyboardShortcuts() {
        keyboard.previewPaneMounted = false
        keyboard.previewHasOutput = false
    }

    private func syncKeyboardShortcuts() {
        keyboard.previewHasOutput = negative.output != nil
        keyboard.cropSessionActive = cropSession.isActive
        keyboard.previewOperationsBlocked =
            edit.isRotating || edit.isDeleting || edit.isSettingTone
            || edit.isSettingColor || edit.isCropping || cropSession.isActive
            || runIsActive
        keyboard.zoomToggleCenter = previewCenter
    }

    private var displayModeButtonHelp: String {
        showsNegative
            ? "Show the positive view (the graded print look)"
            : "Show the underlying negative (raw densities)"
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

    /// Negative identity without pixel generation — a generation-only
    /// change must not reset 100% zoom.
    private var previewNegativeIdentity: String {
        "\(negative.negativeID)#\(negative.previewPath ?? "none")"
    }

    /// Path plus net transform: the CLI rewrites the preview file in
    /// place, so the pair is what tells the thumbnail cache the
    /// contents changed. Deliberately without the display mode: a mode
    /// flip must not reset the zoom, only swap what is fetched (see
    /// `displayIdentity`).
    private var previewIdentity: String {
        "\(previewNegativeIdentity)#\(previewGeneration)"
    }

    /// Everything the fit view's load depends on: the preview identity,
    /// the display mode, and whether crop mode needs the full frame.
    private var displayIdentity: String {
        "\(previewIdentity)#\(displayMode.rawValue)#\(cropSession.isActive)"
    }

    /// The render path — crop mode, negative view, or cached preview —
    /// without the pixel-generation token. Used to avoid blanking the
    /// image when only the pixels change (crop apply, tone edits).
    private var previewModeIdentity: String {
        "\(cropSession.isActive)#\(showsNegative)#\(negative.negativeID)"
    }

    private var previewGeneration: String {
        EditModel.renderGeneration(
            of: negative, cameraColor: edit.roll?.cameraColor, highlightLock: edit.roll?.highlightLock
        )
    }

    private var previewURL: URL? {
        guard let previewPath = negative.previewPath else { return nil }
        return URL(filePath: previewPath)
    }

    /// The fit preview during crop mode: the image rotates about the crop
    /// centre (matching `apply_crop`'s warp) while the overlay stays
    /// axis-aligned. Outside crop mode the image aspect-fits the pane.
    @ViewBuilder
    private func cropAwarePreviewImage(_ image: NSImage, container: CGSize) -> some View {
        if cropSession.isActive, displaySize.width > 0, displaySize.height > 0 {
            let fit = PreviewZoomModel.fitRect(displaySize: displaySize, container: container)
            Color.clear
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .overlay(alignment: .topLeading) {
                    Image(nsImage: image)
                        .resizable()
                        .interpolation(.medium)
                        .frame(width: fit.width, height: fit.height)
                        .rotationEffect(
                            .degrees(cropSession.tiltDegrees),
                            anchor: UnitPoint(
                                x: cropSession.rect.midX / displaySize.width,
                                y: cropSession.rect.midY / displaySize.height
                            )
                        )
                        .offset(x: fit.minX, y: fit.minY)
                }
        } else {
            Image(nsImage: image)
                .resizable()
                .interpolation(.medium)
                .aspectRatio(contentMode: .fit)
        }
    }

    @ViewBuilder
    private var preview: some View {
        GeometryReader { geo in
            ZStack {
                Group {
                    if zoom.mode == .pixels100, negative.output != nil {
                        zoomedCrop
                    } else if let thumbnail {
                        cropAwarePreviewImage(thumbnail.image, container: geo.size)
                    } else if isLoadingPreview && negative.isCompleted {
                        PreviewPlaceholder(kind: .loading)
                    } else if negative.isCompleted {
                        PreviewPlaceholder(kind: .empty)
                    } else {
                        PreviewPlaceholder(kind: .status(negative.status))
                    }
                }
                .id(zoom.mode)
                if showsSpotMarkers {
                    spotMarkers
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
            .clipped()
            .previewDoubleClickZoom(
                enabled: zoomShortcutsEnabled,
                mode: zoom.mode,
                onZoomIn: { scheduleZoomIn(at: $0) },
                onZoomOut: { scheduleZoomOut() }
            )
            .overlay {
                PreviewEventHost(
                    zoom: zoom,
                    zoomEnabled: zoomShortcutsEnabled,
                    cursorRectsEnabled: !cropSession.isActive,
                    onZoomIn: { scheduleZoomIn(at: $0) },
                    onZoomOut: { scheduleZoomOut() },
                    spotHitTester: showsSpotMarkers ? spotMarker(at:) : nil,
                    onSpotToggled: { spotID in toggleSpot(spotID) },
                    onToggleZoom: {
                        guard zoomShortcutsEnabled else { return }
                        scheduleZoomToggle(at: previewCenter)
                    },
                    onApplyCrop: {
                        guard cropSession.isActive, !edit.isCropping,
                            !AppKeyboard.isTextInputFirstResponder()
                        else { return false }
                        applyCrop()
                        return true
                    },
                    onCancelCrop: {
                        guard cropSession.isActive,
                            !AppKeyboard.isTextInputFirstResponder()
                        else { return false }
                        cropSession.end()
                        return true
                    }
                )
                .allowsHitTesting(!cropSession.isActive)
            }
            .overlay {
                if cropSession.isActive, negative.output != nil {
                    CropOverlayView(
                        session: cropSession,
                        fitRect: PreviewZoomModel.fitRect(
                            displaySize: displaySize, container: paneSize
                        ),
                        displaySize: displaySize
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .onChange(of: geo.size, initial: true) { _, size in
                paneSize = size
                Task { @MainActor in
                    refreshZoomContext(paneSize: size)
                }
            }
        }
    }

    // MARK: - Spot markers

    /// Markers show while a set exists and repair is off; with repair on
    /// they hide unless the Heal tab is selected — the point of turning
    /// repair on is to look at the result. Crop mode hides them too: the
    /// live tilt preview rotates the image under an axis-aligned crop box.
    private var showsSpotMarkers: Bool {
        guard let spots = edit.spots, !spots.spots.isEmpty, negative.output != nil else {
            return false
        }
        return (!spots.repair || edit.showsSpotsPopover) && !cropSession.isActive
    }

    /// Accepted spots draw as a thin stroked rect; rejected ones draw
    /// dimmed and dashed, so a rejection is visibly *a decision the user
    /// made*, not a disappearance. The layer takes no events: clicks land
    /// in the event host, which hit-tests through `spotMarker(at:)`.
    @ViewBuilder
    private var spotMarkers: some View {
        ForEach(edit.spots?.spots ?? []) { spot in
            let screen = spotScreenRect(spot.rect)
            RoundedRectangle(cornerRadius: 1)
                .stroke(
                    spot.rejected ? Color.secondary.opacity(0.55) : Color.yellow,
                    style: StrokeStyle(lineWidth: 1.2, dash: spot.rejected ? [3, 3] : [])
                )
                .frame(width: max(screen.width, 3), height: max(screen.height, 3))
                .position(x: screen.midX, y: screen.midY)
                .allowsHitTesting(false)
        }
    }

    /// Display-space rect → screen rect, the same way the image maps: the
    /// fit rect in fit mode, the crop's rect and scale plus the pan offset
    /// at 100%.
    private func spotScreenRect(_ rect: CGRect) -> CGRect {
        switch zoom.mode {
        case .fit:
            return PreviewZoomModel.spotScreenRect(
                rect,
                fitRect: PreviewZoomModel.fitRect(
                    displaySize: displaySize, container: paneSize
                ),
                displaySize: displaySize
            )
        case .pixels100:
            return PreviewZoomModel.spotScreenRect(
                rect,
                viewportOrigin: zoom.origin,
                panOffset: zoom.panOffset,
                displayScale: displayScale,
                paneSize: paneSize,
                viewportSize: PreviewZoomModel.viewportSize(
                    paneSize: paneSize,
                    displayScale: displayScale,
                    displaySize: displaySize
                )
            )
        }
    }

    /// Nearest-rect hit-testing within a small slop radius — markers are
    /// small, so the slop matters. The distance to a rect is to its
    /// nearest edge, zero when the point is inside.
    private func spotMarker(at point: CGPoint) -> Int? {
        let slop: CGFloat = 6
        var bestID: Int?
        var bestDistance = CGFloat.greatestFiniteMagnitude
        for spot in edit.spots?.spots ?? [] {
            let screen = spotScreenRect(spot.rect)
            let distance = Self.rectDistance(screen, to: point)
            if distance <= slop, distance < bestDistance {
                bestDistance = distance
                bestID = spot.id
            }
        }
        return bestID
    }

    /// Nearest-edge distance from a point to a rect, zero when inside
    /// (`CGRect.distance(to:)` is macOS 26-only, so the small arithmetic
    /// lives here).
    private static func rectDistance(_ rect: CGRect, to point: CGPoint) -> CGFloat {
        let dx = max(rect.minX - point.x, 0, point.x - rect.maxX)
        let dy = max(rect.minY - point.y, 0, point.y - rect.maxY)
        return (dx * dx + dy * dy).squareRoot()
    }

    /// Click a marker to toggle its rejection.
    private func toggleSpot(_ spotID: Int) {
        guard let spot = edit.spots?.spots.first(where: { $0.id == spotID }) else { return }
        Task {
            if spot.rejected {
                await edit.acceptSpot(negative, id: spotID)
            } else {
                await edit.rejectSpot(negative, id: spotID)
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
                await edit.renderRegion(negative, rect: rect, mode: displayMode)
            }
        )
        if zoom.mode == .pixels100 { zoom.fetchCrop() }
    }

    /// The 1:1 crop the CLI rendered: one image pixel per physical screen
    /// pixel, drawn hard-edged, translated by the live pan. The overscan
    /// buffer is larger than the pane and offset — clip to the pane so it
    /// cannot paint over the sidebar or toolbar.
    @ViewBuilder
    private var zoomedCrop: some View {
        Color.black
            .overlay {
                ZStack(alignment: .topLeading) {
                    ForEach(zoom.renderedTiles) { rendered in
                        let tile = rendered.tile
                        Image(nsImage: tile.image)
                            .resizable()
                            .interpolation(.none)
                            .frame(
                                width: CGFloat(tile.rect.width) / tile.displayScale,
                                height: CGFloat(tile.rect.height) / tile.displayScale
                            )
                            .offset(zoom.tileScreenOffset(for: tile.rect))
                            .allowsHitTesting(false)
                    }
                    if zoom.viewportIsLoading {
                        PreviewPlaceholder(kind: .loading)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
            .compositingGroup()
            .clipShape(.rect)
            .contentShape(.rect)
    }

    /// The current image's display-space size, when it has been stitched.
    /// Outside crop mode a live crop shrinks the display to the cropped
    /// frame; inside crop mode the overlay always works on the full
    /// uncropped canvas so a saved crop can be adjusted in context.
    private var displaySize: CGSize {
        if cropSession.isActive {
            return uncroppedDisplaySize
        }
        if let crop = negative.crop {
            return CGSize(width: crop.width, height: crop.height)
        }
        return uncroppedDisplaySize
    }

    /// The full uncropped display image's size — quarter turns folded in,
    /// ignoring any live crop.
    private var uncroppedDisplaySize: CGSize {
        if let crop = negative.crop, let canvas = crop.canvasSize {
            return canvas
        }
        guard let output = negative.output else { return .zero }
        return PreviewZoomModel.displaySize(
            tiffSize: CGSize(width: output.width, height: output.height),
            quarterTurns: negative.rotationQuarterTurns
        )
    }

    // MARK: - Crop mode

    private func beginCrop() {
        // Crop editing runs in the fit view on the whole frame — the 1:1
        // zoom's region space is cropped display space, and the overlay
        // belongs to the full uncropped canvas.
        zoom.reset()
        let canvas = uncroppedDisplaySize
        // AC-6: a pending suggest-crop result takes priority over the
        // stored crop — the Auto button sets it.
        if let suggestion = edit.pendingCropSuggestion {
            edit.pendingCropSuggestion = nil
            var preset = suggestion.preset.flatMap(CropPreset.init(rawValue:)) ?? .free
            if preset == .free {
                preset = edit.roll?.captureSetup?.format.flatMap(CropPreset.init(format:)) ?? .free
            }
            cropSession.begin(
                displaySize: canvas,
                rect: suggestion.rect,
                tiltDegrees: suggestion.tiltDegrees,
                preset: preset
            )
        } else if let crop = negative.crop {
            var preset = crop.preset.flatMap(CropPreset.init(rawValue:)) ?? .free
            // AC-6: resolve auto crop's preset from the roll format when
            // the ops-log entry did not carry an explicit label.
            if preset == .free, crop.source == "auto" {
                preset = edit.roll?.captureSetup?.format.flatMap(CropPreset.init(format:)) ?? .free
            }
            cropSession.begin(
                displaySize: canvas,
                rect: crop.editingRect,
                tiltDegrees: crop.tiltDegrees,
                preset: preset
            )
        } else {
            cropSession.begin(displaySize: canvas)
        }
    }

    private func applyCrop() {
        let rect = cropSession.rect
        let tiltDegrees = cropSession.tiltDegrees
        let preset = cropSession.preset == .free ? nil : cropSession.preset.rawValue
        cropSession.end()
        Task {
            await edit.applyCrop(
                negative,
                rect: rect,
                tiltDegrees: tiltDegrees,
                preset: preset,
                fullFrame: true
            )
        }
    }
}

/// The Edit tab's left sidebar: a segmented tab picker and the active
/// adjustment module beneath it.
private struct EditSidebar: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    @Binding var selectedTab: EditSidebarTab
    @Binding var spotsSensitivity: Double
    let targets: [RollManifest.Negative]
    let isMonochromeRoll: Bool
    let runIsActive: Bool
    let cropSession: CropSession
    let displaySize: CGSize
    let onBeginCrop: () -> Void
    let onApplyCrop: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Picker("Adjustments", selection: $selectedTab) {
                ForEach(EditSidebarTab.allCases) { tab in
                    Text(tab.label).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding()
            .help(
                isMonochromeRoll
                    ? "Colour adjustment is unavailable on a monochrome roll"
                    : "Adjustment modules"
            )
            .onChange(of: selectedTab) { _, tab in
                guard !isMonochromeRoll || tab != .color else {
                    selectedTab = .tone
                    return
                }
                // A crop session belongs to the Geometry tab; switching
                // away cancels it.
                if tab != .geometry { cropSession.end() }
                edit.showsSpotsPopover = (tab == .heal)
            }
            .onChange(of: isMonochromeRoll) { _, mono in
                if mono, selectedTab == .color { selectedTab = .tone }
            }
            .onAppear {
                edit.showsSpotsPopover = (selectedTab == .heal)
            }

            ScrollView {
                tabContent
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
            }
        }
        .frame(maxHeight: .infinity)
    }

    @ViewBuilder
    private var tabContent: some View {
        switch selectedTab {
        case .geometry:
            GeometryAdjustmentPanel(
                targets: targets,
                edit: edit,
                runIsActive: runIsActive,
                negative: negative,
                cropSession: cropSession,
                displaySize: displaySize,
                onBeginCrop: onBeginCrop,
                onApplyCrop: onApplyCrop
            )
        case .tone:
            ToneAdjustmentPanel(
                adjustment: negative.toneAdjustment,
                isBusy: edit.isSettingTone || edit.isSettingColor || edit.isRotating
                    || edit.isDeleting || edit.isCropping,
                onScheduleCommit: { adjustment in
                    edit.scheduleTone(targets, adjustment: adjustment)
                },
                onCommitNow: { adjustment, auto in
                    Task {
                        await edit.commitTone(targets, adjustment: adjustment, auto: auto)
                    }
                },
                onReset: {
                    Task { await edit.commitTone(targets, adjustment: nil) }
                }
            )
        case .color:
            ColorAdjustmentPanel(
                adjustment: negative.colorAdjustment,
                isBusy: edit.isSettingColor || edit.isSettingTone || edit.isRotating
                    || edit.isDeleting || edit.isCropping,
                onScheduleCommit: { adjustment in
                    edit.scheduleColor(targets, adjustment: adjustment)
                },
                onCommitNow: { adjustment, auto in
                    Task {
                        await edit.commitColor(targets, adjustment: adjustment, auto: auto)
                    }
                },
                onReset: {
                    Task { await edit.commitColor(targets, adjustment: nil) }
                }
            )
            .disabled(isMonochromeRoll)
        case .heal:
            VStack(alignment: .leading, spacing: 16) {
                ScratchesReviewPanel(
                    negative: negative,
                    edit: edit,
                    isBusy: edit.isDetectingScratches || edit.isTogglingScratches,
                    isMonochromeRoll: isMonochromeRoll
                )
                Divider()
                SpotsReviewPanel(
                    negative: negative,
                    edit: edit,
                    isBusy: edit.isDetectingSpots || edit.isReviewingSpots,
                    sensitivity: $spotsSensitivity
                )
            }
            .onAppear {
                spotsSensitivity = negative.spotsSummary?.sensitivity ?? 0.5
            }
        }
    }
}

/// Rotate, flip, and crop controls for the Geometry sidebar tab. The
/// rotate/flip buttons act on the whole selection; crop mode is
/// anchor-only — the window belongs to the frame
/// the preview shows.
private struct GeometryAdjustmentPanel: View {
    let targets: [RollManifest.Negative]
    @Bindable var edit: EditModel
    let runIsActive: Bool
    let negative: RollManifest.Negative
    @Bindable var cropSession: CropSession
    let displaySize: CGSize
    let onBeginCrop: () -> Void
    let onApplyCrop: () -> Void

    private var isDisabled: Bool {
        edit.isRotating || edit.isDeleting || edit.isSettingTone
            || edit.isSettingColor || edit.isCropping || runIsActive
            || cropSession.isActive
    }

    private var isCropAvailable: Bool {
        negative.output != nil && !isDisabled
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            geometryButton(
                title: "Rotate 90° Counter-clockwise",
                systemImage: "rotate.left",
                help: "Rotate 90° counter-clockwise (⌘[)",
                accessibilityLabel: "Rotate 90° counter-clockwise"
            ) {
                Task { await edit.rotate(targets, clockwise: false) }
            }
            geometryButton(
                title: "Rotate 90° Clockwise",
                systemImage: "rotate.right",
                help: "Rotate 90° clockwise (⌘])",
                accessibilityLabel: "Rotate 90° clockwise"
            ) {
                Task { await edit.rotate(targets, clockwise: true) }
            }
            geometryButton(
                title: "Flip Horizontally",
                systemImage: "arrow.left.and.right.righttriangle.left.righttriangle.right.fill",
                help: "Flip horizontally",
                accessibilityLabel: "Flip horizontally"
            ) {
                Task { await edit.flip(targets) }
            }

            Divider()

            cropSection
        }
    }

    @ViewBuilder
    private var cropSection: some View {
        Text("Crop").font(.headline)
        if cropSession.isActive {
            Picker("Ratio", selection: $cropSession.preset) {
                ForEach(CropPreset.allCases) { preset in
                    Text(preset.label).tag(preset)
                }
            }
            .pickerStyle(.menu)
            .accessibilityLabel("Crop ratio preset")
            .help(
                "Constrain the crop to a film format's gate — the actual "
                    + "frame sizes, oriented to this image"
            )
            .onChange(of: cropSession.preset) { _, _ in
                scheduleCropPresetApply()
            }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Tilt")
                    Spacer()
                    Text(String(format: "%+.1f°", cropSession.tiltDegrees))
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                ToneSlider(
                    value: $cropSession.tiltDegrees,
                    range: CropSession.tiltRange,
                    step: CropSession.tiltStep,
                    resetValue: 0,
                    onScheduleCommit: {},
                    onCommitNow: {}
                )
                .accessibilityLabel("Crop tilt")
                Text("Counter-clockwise rotation of the image under the crop, ±10°")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack {
                Button("Apply") { onApplyCrop() }
                    .disabled(edit.isCropping)
                    .keyboardShortcut(.defaultAction)
                    .help(
                        "Record the crop (Return; the published TIFF is untouched; "
                            + "the export bakes it in)"
                    )
                Button("Cancel") { cropSession.end() }
                    .keyboardShortcut(.cancelAction)
                    .help("Discard this crop session (Escape)")
                Spacer()
                if edit.isCropping {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            Button("Original") { cropSession.resetToOriginal() }
                .disabled(edit.isCropping)
                .help("Reset the crop window to the full image and clear the ratio preset")

            Button("Auto") {
                Task { await edit.suggestCrop(negative) }
            }
            .disabled(edit.isCropping)
            .help("Detect the picture boundary and fit a crop to the roll's film format")
        } else {
            Button {
                onBeginCrop()
            } label: {
                Label("Crop…", systemImage: "crop")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .disabled(!isCropAvailable)
            .help("Crop this negative (⌘R; ratio presets and tilt in the overlay session)")
            .accessibilityLabel("Crop")
        }
    }

    /// Defers preset reshaping out of the picker's update pass — mutating
    /// the overlay rect synchronously from the binding has been observed to
    /// crash SwiftUI's observation pass.
    private func scheduleCropPresetApply() {
        Task { @MainActor in
            cropSession.applyPreset()
        }
    }

    private func geometryButton(
        title: String,
        systemImage: String,
        help: String,
        accessibilityLabel: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .disabled(isDisabled)
        .help(help)
        .accessibilityLabel(accessibilityLabel)
    }
}

/// The Edit tab's tone adjustment panel, grouped like NegPy's Exposure panel.
private struct ToneAdjustmentPanel: View {
    let adjustment: ToneAdjustment?
    let isBusy: Bool
    let onScheduleCommit: (_ adjustment: ToneAdjustment) -> Void
    let onCommitNow: (_ adjustment: ToneAdjustment, _ auto: ToneAutoFlags) -> Void
    let onReset: () -> Void

    private static let snapRange: ClosedRange<Double> = -0.8...1.5
    private static let densityRange: ClosedRange<Double> = 0...2
    private static let shadowDensityRange: ClosedRange<Double> = -0.9...0.9
    private static let highlightDensityRange: ClosedRange<Double> = -0.5...0.5

    @State private var values = ToneAdjustment.neutral
    @State private var isDragging = false

    private func inverted(_ value: Double, in range: ClosedRange<Double>) -> Double {
        range.upperBound + range.lowerBound - value
    }

    private var brightness: Double {
        get { inverted(values.density, in: Self.densityRange) }
        nonmutating set { values.density = inverted(newValue, in: Self.densityRange) }
    }

    private var shadows: Double {
        get { inverted(values.shadowDensity, in: Self.shadowDensityRange) }
        nonmutating set { values.shadowDensity = inverted(newValue, in: Self.shadowDensityRange) }
    }

    private var highlights: Double {
        get { inverted(values.highlightDensity, in: Self.highlightDensityRange) }
        nonmutating set { values.highlightDensity = inverted(newValue, in: Self.highlightDensityRange) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            sectionHeader("Tone")
                sliderRow(
                    title: "Brightness",
                    valueLabel: String(format: "%.2f", brightness),
                    value: Binding(get: { brightness }, set: { brightness = $0 }),
                    range: Self.densityRange,
                    step: 0.05,
                    resetValue: inverted(ToneAdjustment.neutral.density, in: Self.densityRange),
                    accessibilityLabel: "Brightness",
                    help: "0.0–2.0, higher is brighter",
                    autoHelp: "Solve brightness from this negative's own metering",
                    autoFlag: .density
                )
                sliderRow(
                    title: "Contrast",
                    valueLabel: String(format: "%+.2f", values.snapGamma),
                    value: $values.snapGamma,
                    range: Self.snapRange,
                    step: 0.05,
                    resetValue: ToneAdjustment.neutral.snapGamma,
                    accessibilityLabel: "Contrast",
                    help: "−0.8–1.5, midtone contrast"
                )

                sectionHeader("Zones")
                sliderRow(
                    title: "Shadows",
                    valueLabel: String(format: "%+.2f", shadows),
                    value: Binding(get: { shadows }, set: { shadows = $0 }),
                    range: Self.shadowDensityRange,
                    step: 0.05,
                    resetValue: 0,
                    accessibilityLabel: "Shadows",
                    help: "±0.9, positive brightens"
                )
                sliderRow(
                    title: "Highlights",
                    valueLabel: String(format: "%+.2f", highlights),
                    value: Binding(get: { highlights }, set: { highlights = $0 }),
                    range: Self.highlightDensityRange,
                    step: 0.05,
                    resetValue: 0,
                    accessibilityLabel: "Highlights",
                    help: "±0.5, positive brightens"
                )

                Divider()

                HStack {
                    Button("Reset") {
                        values = ToneAdjustment.neutral
                        onReset()
                    }
                    .disabled(isBusy)
                    .help("Return every control to the default scan-start curve")
                    Spacer()
                    if isBusy {
                        ProgressView()
                            .controlSize(.small)
                    }
                }
        }
        .onAppear { syncFromModel() }
        .onChange(of: adjustment) {
            guard !isDragging else { return }
            syncFromModel()
        }
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
                        commitNow(auto: autoFlag)
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
                onEditingChanged: { isDragging = $0 },
                onScheduleCommit: scheduleCommit,
                onCommitNow: { commitNow(auto: []) }
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

    private func commitNow(auto: ToneAutoFlags) {
        if snappedValues == ToneAdjustment.neutral && auto.isEmpty {
            onReset()
        } else {
            onCommitNow(snappedValues, auto)
        }
    }

    private var snappedValues: ToneAdjustment {
        ToneAdjustment(
            snapGamma: ToneSlider.snap(values.snapGamma, step: 0.05, range: Self.snapRange),
            density: ToneSlider.snap(values.density, step: 0.05, range: Self.densityRange),
            shadowDensity: ToneSlider.snap(
                values.shadowDensity, step: 0.05, range: Self.shadowDensityRange
            ),
            highlightDensity: ToneSlider.snap(
                values.highlightDensity, step: 0.05, range: Self.highlightDensityRange
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
    var trackColors: [Color]? = nil
    var onEditingChanged: ((Bool) -> Void)? = nil
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
            onEditingChanged?(editing)
            guard !editing else { return }
            onCommitNow()
        }
        .background(alignment: .center) {
            if let trackColors {
                SliderTrackBackground(colors: trackColors)
            }
        }
        .doubleClickReset {
            value = resetValue
            onCommitNow()
        }
    }
}

private struct SliderTrackBackground: View {
    let colors: [Color]

    var body: some View {
        Capsule()
            .fill(
                LinearGradient(
                    colors: colors,
                    startPoint: .leading,
                    endPoint: .trailing
                )
            )
            .frame(height: 4)
            .padding(.horizontal, 7)
    }
}

private enum BalanceTrackColors {
    static let warmth: [Color] = [
        Color(red: 0.35, green: 0.55, blue: 0.95),
        Color(red: 1.0, green: 0.82, blue: 0.25),
    ]
    static let tint: [Color] = [
        Color(red: 0.35, green: 0.90, blue: 0.35),
        Color(red: 0.95, green: 0.20, blue: 0.85),
    ]
}

private struct ChannelCurvesEditor: View {
    @Binding var values: ColorAdjustment
    let onScheduleCommit: () -> Void
    let onCommitNow: () -> Void

    enum Channel: String, CaseIterable, Identifiable {
        case red, green, blue, all
        var id: String { rawValue }
        var label: String { rawValue.capitalized }
    }

    @State private var selectedChannel: Channel = .red
    /// The point being dragged, set from the nearest point to the mouse
    /// down location and held until the gesture ends.
    @State private var draggedPointIndex: Int?
    /// Set instead of `draggedPointIndex` when the drag started ⌥-held,
    /// so the whole channel resets on release rather than one point moving.
    @State private var isResettingChannel = false

    private var offsets: Binding<(q1: Double, mid: Double, q3: Double)> {
        Binding(
            get: {
                switch selectedChannel {
                case .red: (values.curveRed25, values.curveRed50, values.curveRed75)
                case .green: (values.curveGreen25, values.curveGreen50, values.curveGreen75)
                case .blue: (values.curveBlue25, values.curveBlue50, values.curveBlue75)
                case .all: (0, 0, 0)
                }
            },
            set: { new in
                switch selectedChannel {
                case .red:
                    values.curveRed25 = new.q1
                    values.curveRed50 = new.mid
                    values.curveRed75 = new.q3
                case .green:
                    values.curveGreen25 = new.q1
                    values.curveGreen50 = new.mid
                    values.curveGreen75 = new.q3
                case .blue:
                    values.curveBlue25 = new.q1
                    values.curveBlue50 = new.mid
                    values.curveBlue75 = new.q3
                case .all: break
                }
                onScheduleCommit()
            }
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Curves").font(.headline)
                Spacer()
                Picker("Channel", selection: $selectedChannel) {
                    ForEach(Channel.allCases) { item in
                        Text(item.label).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 220)
            }
            curveCanvas
                .frame(height: 160)
                .clipShape(RoundedRectangle(cornerRadius: 4))
                .overlay(
                    RoundedRectangle(cornerRadius: 4)
                        .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                )
            if selectedChannel != .all {
                curvePointControls
            }
            Button("Reset Curves") {
                resetCurves()
                onCommitNow()
            }
            .buttonStyle(.borderless)
        }
    }

    private var curveCanvas: some View {
        GeometryReader { geometry in
            Canvas { context, size in
                let w = size.width
                let h = size.height
                // Grid lines
                let gridColor = Color.secondary.opacity(0.15)
                for i in 1...3 {
                    let x = w * Double(i) / 4.0
                    context.stroke(Path(CGRect(x: x, y: 0, width: 0, height: h)), with: .color(gridColor))
                    let y = h * Double(i) / 4.0
                    context.stroke(Path(CGRect(x: 0, y: y, width: w, height: 0)), with: .color(gridColor))
                }
                // Identity line
                var identity = Path()
                identity.move(to: CGPoint(x: 0, y: h))
                identity.addLine(to: CGPoint(x: w, y: 0))
                context.stroke(identity, with: .color(Color.secondary.opacity(0.25)), lineWidth: 1)
                // Draw curves
                let channels: [(Color, (Double, Double, Double))] = selectedChannel == .all
                    ? [
                        (.red, (values.curveRed25, values.curveRed50, values.curveRed75)),
                        (.green, (values.curveGreen25, values.curveGreen50, values.curveGreen75)),
                        (.blue, (values.curveBlue25, values.curveBlue50, values.curveBlue75)),
                    ]
                    : [(
                        channelColor(selectedChannel),
                        (offsets.wrappedValue.q1, offsets.wrappedValue.mid, offsets.wrappedValue.q3)
                    )]
                for (color, off) in channels {
                    let y = ChannelCurve.yKnots(off)
                    let slopes = ChannelCurve.slopes(x: ChannelCurve.xKnots, y: y)
                    var path = Path()
                    let steps = 64
                    for i in 0...steps {
                        let t = Double(i) / Double(steps)
                        let value = ChannelCurve.evaluate(t: t, x: ChannelCurve.xKnots, y: y, slopes: slopes)
                        let px = t * w
                        let py = (1.0 - value) * h
                        if i == 0 {
                            path.move(to: CGPoint(x: px, y: py))
                        } else {
                            path.addLine(to: CGPoint(x: px, y: py))
                        }
                    }
                    context.stroke(path, with: .color(color), lineWidth: 2)
                }
                // Control points
                if selectedChannel != .all {
                    let off = offsets.wrappedValue
                    let pts = [
                        (0.25, 0.25 + off.q1),
                        (0.5, 0.5 + off.mid),
                        (0.75, 0.75 + off.q3),
                    ]
                    let color = channelColor(selectedChannel)
                    for (px, py) in pts {
                        let center = CGPoint(x: px * w, y: (1.0 - py) * h)
                        let rect = CGRect(x: center.x - 5, y: center.y - 5, width: 10, height: 10)
                        context.fill(Path(ellipseIn: rect), with: .color(color))
                        context.stroke(Path(ellipseIn: rect), with: .color(.white), lineWidth: 1.5)
                    }
                }
            }
            .contentShape(Rectangle())
            .gesture(dragGesture(in: geometry.size))
            .highPriorityGesture(doubleClickGesture(in: geometry.size))
        }
    }

    /// Drags the nearest point (by x) vertically. Starting the drag with
    /// ⌥ held resets the whole channel on release instead of moving a
    /// point. No-op in "All" mode, where no points are editable.
    private func dragGesture(in size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                guard selectedChannel != .all else { return }
                if draggedPointIndex == nil, !isResettingChannel {
                    if NSEvent.modifierFlags.contains(.option) {
                        isResettingChannel = true
                        return
                    }
                    draggedPointIndex = nearestPointIndex(to: value.startLocation, size: size)
                }
                guard !isResettingChannel, let index = draggedPointIndex else { return }
                setOffset(index, offset(atLocation: value.location, size: size, index: index))
            }
            .onEnded { _ in
                defer {
                    draggedPointIndex = nil
                    isResettingChannel = false
                }
                if isResettingChannel {
                    resetCurves()
                    onCommitNow()
                } else if draggedPointIndex != nil {
                    onCommitNow()
                }
            }
    }

    /// Double-clicking a point resets it. No-op in "All" mode.
    private func doubleClickGesture(in size: CGSize) -> some Gesture {
        SpatialTapGesture(count: 2)
            .onEnded { value in
                guard selectedChannel != .all else { return }
                setOffset(nearestPointIndex(to: value.location, size: size), 0)
                onCommitNow()
            }
    }

    private func nearestPointIndex(to location: CGPoint, size: CGSize) -> Int {
        guard size.width > 0 else { return 0 }
        let t = Double(location.x / size.width)
        let xs = Array(ChannelCurve.xKnots[1...3])
        var best = 0
        var bestDistance = Double.greatestFiniteMagnitude
        for (index, x) in xs.enumerated() {
            let distance = abs(x - t)
            if distance < bestDistance {
                bestDistance = distance
                best = index
            }
        }
        return best
    }

    /// The clamped offset that puts control point `index` at `location`'s
    /// height.
    private func offset(atLocation location: CGPoint, size: CGSize, index: Int) -> Double {
        guard size.height > 0 else { return 0 }
        let displayValue = 1.0 - Double(location.y / size.height)
        let base = ChannelCurve.xKnots[index + 1]
        return ChannelCurve.clampOffset(displayValue - base, at: index, other: offsets.wrappedValue)
    }

    private func setOffset(_ index: Int, _ value: Double) {
        switch index {
        case 0: offsets.wrappedValue.q1 = value
        case 1: offsets.wrappedValue.mid = value
        default: offsets.wrappedValue.q3 = value
        }
    }

    private var curvePointControls: some View {
        VStack(spacing: 6) {
            curvePointRow("25%", index: 0)
            curvePointRow("50%", index: 1)
            curvePointRow("75%", index: 2)
        }
    }

    private func curvePointRow(_ label: String, index: Int) -> some View {
        HStack {
            Text(label)
                .monospacedDigit()
                .frame(width: 30)
            ToneSlider(
                value: pointOffsetBinding(index),
                range: -ChannelCurve.offsetMax...ChannelCurve.offsetMax,
                step: 0.01,
                resetValue: 0,
                onScheduleCommit: onScheduleCommit,
                onCommitNow: onCommitNow
            )
            Text(String(format: "%+.2f", pointOffsetBinding(index).wrappedValue))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 45, alignment: .trailing)
        }
    }

    private func pointOffsetBinding(_ index: Int) -> Binding<Double> {
        Binding(
            get: {
                switch index {
                case 0: offsets.wrappedValue.q1
                case 1: offsets.wrappedValue.mid
                default: offsets.wrappedValue.q3
                }
            },
            set: { newValue in
                let clamped = ChannelCurve.clampOffset(newValue, at: index, other: offsets.wrappedValue)
                setOffset(index, clamped)
            }
        )
    }

    /// Only ever called for a single selected channel — "All" mode draws
    /// its three curves with fixed colors and has no editable points.
    private func channelColor(_ channel: Channel) -> Color {
        switch channel {
        case .red: .red
        case .green: .green
        case .blue: .blue
        case .all: preconditionFailure("channelColor(.all) is unreachable")
        }
    }

    private func resetCurves() {
        switch selectedChannel {
        case .red:
            values.curveRed25 = 0; values.curveRed50 = 0; values.curveRed75 = 0
        case .green:
            values.curveGreen25 = 0; values.curveGreen50 = 0; values.curveGreen75 = 0
        case .blue:
            values.curveBlue25 = 0; values.curveBlue50 = 0; values.curveBlue75 = 0
        case .all:
            values.curveRed25 = 0; values.curveRed50 = 0; values.curveRed75 = 0
            values.curveGreen25 = 0; values.curveGreen50 = 0; values.curveGreen75 = 0
            values.curveBlue25 = 0; values.curveBlue50 = 0; values.curveBlue75 = 0
        }
    }
}

private struct ColorAdjustmentPanel: View {
    let adjustment: ColorAdjustment?
    let isBusy: Bool
    let onScheduleCommit: (_ adjustment: ColorAdjustment) -> Void
    let onCommitNow: (_ adjustment: ColorAdjustment, _ auto: ColorAutoFlags) -> Void
    let onReset: () -> Void

    @State private var values = ColorAdjustment.neutral

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Color").font(.headline)

            colorSlider(
                "Warmth",
                value: $values.warmth,
                range: -1...1,
                step: 0.02,
                resetValue: ColorAdjustment.neutral.warmth,
                trackColors: BalanceTrackColors.warmth
            ) {
                String(format: "%+.2f", values.warmth)
            }
            .help("Blue (−) to yellow (+); lightness-neutral")

            colorSlider(
                "Tint",
                value: $values.tint,
                range: -1...1,
                step: 0.02,
                resetValue: ColorAdjustment.neutral.tint,
                trackColors: BalanceTrackColors.tint
            ) {
                String(format: "%+.2f", values.tint)
            }
            .help("Green (−) to magenta (+); lightness-neutral")

            Button {
                onCommitNow(values, .balance)
            } label: {
                Label("Auto", systemImage: "wand.and.stars")
            }
            .buttonStyle(.borderless)
            .disabled(isBusy)
            .help("Solve the balance from this negative's own neutral estimate")

            ChannelCurvesEditor(
                values: $values,
                onScheduleCommit: { onScheduleCommit(values) },
                onCommitNow: { onCommitNow(values, []) }
            )

            Text("Correction").font(.headline)
            colorSlider(
                "Cast Removal — Shadows",
                value: $values.castRemoval,
                range: 0...1,
                step: 0.05,
                resetValue: ColorAdjustment.neutral.castRemoval
            ) {
                String(format: "%.2f", values.castRemoval)
            }
            .help("Balances each layer against the frame's own shadow greys.")
            colorSlider(
                "Cast Removal — Highlights",
                value: $values.castRemovalHighlights,
                range: 0...1,
                step: 0.05,
                resetValue: ColorAdjustment.neutral.castRemovalHighlights
            ) {
                String(format: "%.2f", values.castRemovalHighlights)
            }
            .help(
                "Balances each layer against the frame's own highlight greys. "
                    + "Needs a highlight reference; inactive on rolls stitched before it was measured."
            )

            Text("Saturation").font(.headline)
            colorSlider(
                "Dye Separation",
                value: $values.dyeSeparation,
                range: 0.5...1.5,
                step: 0.02,
                resetValue: ColorAdjustment.neutral.dyeSeparation
            ) {
                String(format: "%.2f", values.dyeSeparation)
            }
            colorSlider(
                "Separation Damping",
                value: $values.separationDamping,
                range: 0...1,
                step: 0.05,
                resetValue: ColorAdjustment.neutral.separationDamping,
                disabled: values.dyeSeparation == 1
            ) {
                String(format: "%.2f", values.separationDamping)
            }
            .help(
                "Amplifies muted color and eases already-vivid areas; "
                    + "inactive at neutral separation"
            )

            Button("Reset All", role: .destructive) { onReset() }
        }
        .disabled(isBusy)
        .onAppear(perform: syncFromModel)
        .onChange(of: adjustment) { syncFromModel() }
    }

    private func colorSlider(
        _ title: String,
        value: Binding<Double>,
        range: ClosedRange<Double>,
        step: Double,
        resetValue: Double,
        disabled: Bool = false,
        trackColors: [Color]? = nil,
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
                resetValue: resetValue,
                trackColors: trackColors,
                onScheduleCommit: { onScheduleCommit(values) },
                onCommitNow: { onCommitNow(values, []) }
            )
            .disabled(disabled)
        }
    }

    private func syncFromModel() {
        values = adjustment ?? ColorAdjustment.neutral
    }
}

private struct ScratchesReviewPanel: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    let isBusy: Bool
    let isMonochromeRoll: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Scratches")
                .font(.headline)
            Toggle("Remove scratches", isOn: removalBinding)
                .disabled(isBusy || isMonochromeRoll || !canToggle)
            captionRow
        }
    }

    private var summary: NegativeScratches.Summary? { negative.scratchesSummary }

    private var canToggle: Bool {
        summary != nil && summary?.stale == false
    }

    private var removalBinding: Binding<Bool> {
        Binding(
            get: { summary?.enabled ?? false },
            set: { newValue in
                guard newValue != (summary?.enabled ?? false) else { return }
                Task {
                    await edit.setScratchRemoval(edit.selectionTargets, on: newValue)
                }
            }
        )
    }

    @ViewBuilder
    private var captionRow: some View {
        if isMonochromeRoll {
            Text("Colour film only")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else if let summary {
            if summary.stale {
                HStack {
                    Text("Stale — analyse again")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    analyseButton
                }
            } else if summary.count > 0 {
                Text("\(summary.count) found")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("None found")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else {
            HStack {
                Text("Not analysed")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                analyseButton
            }
        }
    }

    private var analyseButton: some View {
        Button("Analyse") {
            Task { await edit.detectScratches(edit.selectionTargets) }
        }
        .disabled(isBusy || edit.selectionTargets.isEmpty || isMonochromeRoll)
    }
}

/// The Heal sidebar panel: the sensitivity slider,
/// the counts, the whole-negative repair toggle, and Clear. Detect is a
/// selection-level command but the panel reviews the displayed negative —
/// `edit.selectionTargets` is what a Detect click sends.
private struct SpotsReviewPanel: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    let isBusy: Bool
    @Binding var sensitivity: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Sensitivity")
                    Spacer()
                    Text(String(format: "%.2f", sensitivity))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                Slider(value: $sensitivity, in: 0...1, step: 0.05)
                    .doubleClickReset {
                        sensitivity = 0.5
                    }
                Text("0.0 misses crud before risking detail; 1.0 proposes aggressively.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Button {
                Task {
                    await edit.detectSpots(edit.selectionTargets, sensitivity: sensitivity)
                }
            } label: {
                Label("Find Spots", systemImage: "sparkle.magnifyingglass")
            }
            .disabled(isBusy || edit.selectionTargets.isEmpty)

            countsRow

            Toggle("Repair", isOn: repairBinding)
                .disabled(isBusy || !hasAcceptableSpots)
                .help(
                    repairOn
                        ? "Inpainting is live — the preview shows the repaired pixels"
                        : "Inpaint every accepted spot's exact mask"
                )

            HStack {
                Button("Clear", role: .destructive) {
                    Task { await edit.clearSpots(negative) }
                }
                .disabled(isBusy || !hasSpots)
                .help("Remove the spot set and switch repair off")
                Spacer()
                if isBusy {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
    }

    private var spots: NegativeSpots? { edit.spots }
    private var summary: NegativeSpots.Summary? { negative.spotsSummary }

    private var repairOn: Bool {
        spots?.repair ?? summary?.repair ?? false
    }

    private var hasSpots: Bool {
        (spots?.spots.isEmpty == false) || ((summary?.count ?? 0) > 0)
    }

    private var hasAcceptableSpots: Bool {
        (spots?.accepted.isEmpty == false)
            || ((summary.map { $0.count - $0.rejected } ?? 0) > 0)
    }

    private var countsRow: some View {
        let text: String
        if let spots {
            let rejected = spots.spots.count - spots.accepted.count
            text = spots.spots.isEmpty
                ? "No spots found"
                : "\(spots.spots.count) found, \(rejected) rejected"
        } else if let summary, summary.count > 0 {
            text = summary.stale
                ? "Stale — the negative was re-stitched; find spots again"
                : "\(summary.count) found, \(summary.rejected) rejected"
        } else {
            text = "No spots found"
        }
        return HStack {
            Text(text)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }

    private var repairBinding: Binding<Bool> {
        Binding(
            get: { repairOn },
            set: { on in
                Task { await edit.setRepair(negative, on: on) }
            }
        )
    }
}

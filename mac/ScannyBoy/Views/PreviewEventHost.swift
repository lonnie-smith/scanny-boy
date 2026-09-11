import AppKit
import SwiftUI

/// The Edit tab preview's AppKit event host: swaps the cursor to a hand at
/// 100%, translates plain drag into pans at 100%, and handles firm
/// trackpad/mouse double-clicks for fit ↔ 100% zoom.
///
/// SwiftUI cannot filter a drag on a *held* key the way it filters on
/// `.shift`, so the pan gesture lives on an NSView overlaid on the preview.
/// Plain clicks pass straight through — nothing else on the tab needs them,
/// except the spot markers: a plain click that hits a marker toggles its
/// rejection, and one that hits nothing keeps doing nothing. Light
/// trackpad double-taps are handled separately via `previewDoubleClickZoom`.
struct PreviewEventHost: NSViewRepresentable {
    /// `PreviewZoomModel` is `@MainActor`, like every `NSView`; the host
    /// only touches it from event callbacks and cursor updates, which all
    /// run on the main thread.
    let zoom: PreviewZoomModel
    var zoomEnabled: Bool = true
    /// When false, leave cursor rects alone so the crop overlay owns them.
    var cursorRectsEnabled: Bool = true
    var onZoomIn: (@MainActor (CGPoint) -> Void)? = nil
    var onZoomOut: (@MainActor () -> Void)? = nil
    /// The spot id under `panePoint`, or nil — the marker layer only takes
    /// a click when a marker is under the cursor (nearest-rect within a
    /// slop radius).
    var spotHitTester: (@MainActor (CGPoint) -> Int?)? = nil
    /// Called with the toggled spot's id when a marker click lands.
    var onSpotToggled: (@MainActor (Int) -> Void)? = nil
    /// Called when ⌘Z is pressed while this preview is on screen.
    var onToggleZoom: (@MainActor () -> Void)? = nil
    /// Called when Return is pressed; return true to consume the key.
    var onApplyCrop: (@MainActor () -> Bool)? = nil
    /// Called when Escape is pressed; return true to consume the key.
    var onCancelCrop: (@MainActor () -> Bool)? = nil

    func makeNSView(context: Context) -> PreviewEventView {
        PreviewEventView(
            zoom: zoom,
            zoomEnabled: zoomEnabled,
            cursorRectsEnabled: cursorRectsEnabled,
            onZoomIn: onZoomIn,
            onZoomOut: onZoomOut,
            spotHitTester: spotHitTester,
            onSpotToggled: onSpotToggled,
            onToggleZoom: onToggleZoom,
            onApplyCrop: onApplyCrop,
            onCancelCrop: onCancelCrop
        )
    }

    func updateNSView(_ view: PreviewEventView, context: Context) {
        view.model = zoom
        view.zoomEnabled = zoomEnabled
        view.cursorRectsEnabled = cursorRectsEnabled
        view.onZoomIn = onZoomIn
        view.onZoomOut = onZoomOut
        view.spotHitTester = spotHitTester
        view.onSpotToggled = onSpotToggled
        view.onToggleZoom = onToggleZoom
        view.onApplyCrop = onApplyCrop
        view.onCancelCrop = onCancelCrop
        // Defer cursor invalidation out of SwiftUI's layout pass — calling
        // it synchronously from `updateNSView` while the preview is swapping
        // between fit and 100% has been observed to destabilize AppKit.
        DispatchQueue.main.async {
            view.window?.invalidateCursorRects(for: view)
        }
    }
}

/// The `NSView` behind `PreviewEventHost`.
@MainActor
final class PreviewEventView: NSView {
    var model: PreviewZoomModel
    var zoomEnabled: Bool
    var cursorRectsEnabled: Bool
    var onZoomIn: (@MainActor (CGPoint) -> Void)?
    var onZoomOut: (@MainActor () -> Void)?
    var spotHitTester: (@MainActor (CGPoint) -> Int?)?
    var onSpotToggled: (@MainActor (Int) -> Void)?
    var onToggleZoom: (@MainActor () -> Void)?
    var onApplyCrop: (@MainActor () -> Bool)?
    var onCancelCrop: (@MainActor () -> Bool)?

    init(
        zoom: PreviewZoomModel,
        zoomEnabled: Bool = true,
        cursorRectsEnabled: Bool = true,
        onZoomIn: (@MainActor (CGPoint) -> Void)? = nil,
        onZoomOut: (@MainActor () -> Void)? = nil,
        spotHitTester: (@MainActor (CGPoint) -> Int?)? = nil,
        onSpotToggled: (@MainActor (Int) -> Void)? = nil,
        onToggleZoom: (@MainActor () -> Void)? = nil,
        onApplyCrop: (@MainActor () -> Bool)? = nil,
        onCancelCrop: (@MainActor () -> Bool)? = nil
    ) {
        self.model = zoom
        self.zoomEnabled = zoomEnabled
        self.cursorRectsEnabled = cursorRectsEnabled
        self.onZoomIn = onZoomIn
        self.onZoomOut = onZoomOut
        self.spotHitTester = spotHitTester
        self.onSpotToggled = onSpotToggled
        self.onToggleZoom = onToggleZoom
        self.onApplyCrop = onApplyCrop
        self.onCancelCrop = onCancelCrop
        super.init(frame: .zero)
        clipsToBounds = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("PreviewEventView is created by PreviewEventHost only")
    }

    /// Match SwiftUI's top-left coordinate space for mouse points.
    override var isFlipped: Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let superview else { return nil }
        let local = convert(point, from: superview)
        return bounds.contains(local) ? super.hitTest(point) : nil
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window != nil {
            installKeyMonitor()
        } else {
            uninstallKeyMonitor()
        }
    }

    override func viewDidUnhide() {
        super.viewDidUnhide()
        window?.invalidateCursorRects(for: self)
    }

    // MARK: - Keyboard

    private var keyMonitor: Any?

    /// The uninstall for the monitor above. `viewDidMoveToWindow(nil)` runs
    /// when the representable's view leaves the hierarchy, which covers the
    /// teardown — deinit is nonisolated under Swift 6 and cannot touch
    /// main-actor state, so the monitor is never removed there.
    private func uninstallKeyMonitor() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
        }
        keyMonitor = nil
    }

    private static let zKeyCode: UInt16 = 6
    private static let returnKeyCode: UInt16 = 36
    private static let enterKeyCode: UInt16 = 76
    private static let escapeKeyCode: UInt16 = 53

    /// Handles ⌘Z for fit ↔ 100%, Return to apply a crop session, and
    /// Escape to cancel one — without waiting for menu focus.
    /// Installed in `viewDidMoveToWindow`, so these work as soon as the
    /// preview appears — not only after a click.
    private func installKeyMonitor() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            if event.keyCode == Self.zKeyCode,
                event.modifierFlags.contains(.command),
                !event.modifierFlags.contains(.option),
                !event.modifierFlags.contains(.control)
            {
                self.onToggleZoom?()
                return nil
            }
            if event.keyCode == Self.returnKeyCode || event.keyCode == Self.enterKeyCode,
                event.modifierFlags.intersection([.command, .option, .control, .shift]).isEmpty,
                self.onApplyCrop?() == true
            {
                return nil
            }
            if event.keyCode == Self.escapeKeyCode,
                self.onCancelCrop?() == true
            {
                return nil
            }
            return event
        }
    }

    // MARK: - Mouse

    private var gestureIsActive = false
    private var gestureStart: CGPoint?
    private var pendingSpotClick: DispatchWorkItem?

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            cancelPendingSpotClick()
            if gestureIsActive {
                model.mouseUp(at: gestureStart ?? point(for: event))
                gestureIsActive = false
                gestureStart = nil
                window?.invalidateCursorRects(for: self)
            }
            guard zoomEnabled else { return }
            let pt = point(for: event)
            switch model.mode {
            case .fit:
                onZoomIn?(pt)
            case .pixels100:
                onZoomOut?()
            }
            return
        }
        if model.mode == .pixels100 {
            gestureIsActive = true
            gestureStart = point(for: event)
            window?.invalidateCursorRects(for: self)
            model.mouseDown(at: point(for: event))
            return
        }
        super.mouseDown(with: event)
    }

    override func mouseDragged(with event: NSEvent) {
        guard gestureIsActive else {
            super.mouseDragged(with: event)
            return
        }
        model.mouseDragged(to: point(for: event))
    }

    override func mouseUp(with event: NSEvent) {
        if gestureIsActive {
            let end = point(for: event)
            let wasClick = gestureStart.map { start in
                abs(end.x - start.x) < PreviewZoomModel.clickTolerance
                    && abs(end.y - start.y) < PreviewZoomModel.clickTolerance
            } ?? false
            gestureIsActive = false
            gestureStart = nil
            window?.invalidateCursorRects(for: self)
            model.mouseUp(at: end)
            if wasClick, model.mode == .pixels100 {
                tryToggleSpot(at: end)
            }
            return
        }
        scheduleSpotClick(at: point(for: event))
    }

    /// A plain click (no active gesture): a marker under the cursor toggles
    /// its rejection; empty space keeps doing nothing.
    private func tryToggleSpot(at panePoint: CGPoint) {
        if let spotHitTester, let onSpotToggled,
            let spotID = spotHitTester(panePoint)
        {
            onSpotToggled(spotID)
        }
    }

    /// Defers fit-mode spot toggles so the first click of a double-click
    /// does not fire before the second click arrives.
    private func scheduleSpotClick(at panePoint: CGPoint) {
        guard model.mode == .fit else {
            tryToggleSpot(at: panePoint)
            return
        }
        cancelPendingSpotClick()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingSpotClick = nil
            self.tryToggleSpot(at: panePoint)
        }
        pendingSpotClick = work
        DispatchQueue.main.asyncAfter(
            deadline: .now() + NSEvent.doubleClickInterval,
            execute: work
        )
    }

    private func cancelPendingSpotClick() {
        pendingSpotClick?.cancel()
        pendingSpotClick = nil
    }

    // MARK: - Cursor

    /// 100% → open/closed hand; otherwise the plain arrow.
    override func resetCursorRects() {
        guard cursorRectsEnabled else { return }
        if model.mode == .pixels100 {
            let hand = gestureIsActive ? NSCursor.closedHand : NSCursor.openHand
            addCursorRect(bounds, cursor: hand)
            return
        }
        super.resetCursorRects()
    }

    private func point(for event: NSEvent) -> CGPoint {
        convert(event.locationInWindow, from: nil)
    }
}

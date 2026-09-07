import AppKit
import SwiftUI

/// The Edit tab preview's AppKit event host: swaps the cursor to a hand at
/// 100% (or a magnifier at fit when ⌘Space is held), and translates plain
/// drag into pans at 100% and ⌘Space+click into zoom-in from fit.
///
/// SwiftUI cannot filter a drag on a *held* key the way it filters on
/// `.shift`, so the pan gesture lives on an NSView overlaid on the preview.
/// Plain clicks pass straight through — nothing else on the tab needs them,
/// except the spot markers (SPOTTING_PLAN §8.3): a plain click that hits a
/// marker toggles its rejection, and one that hits nothing keeps doing
/// nothing (⌘Space+click zoom lives on the zoom-in gesture).
struct PreviewEventHost: NSViewRepresentable {
    /// `PreviewZoomModel` is `@MainActor`, like every `NSView`; the host
    /// only touches it from event callbacks and cursor updates, which all
    /// run on the main thread.
    let zoom: PreviewZoomModel
    /// The spot id under `panePoint`, or nil — the marker layer only takes
    /// a click when a marker is under the cursor (nearest-rect within a
    /// slop radius).
    var spotHitTester: (@MainActor (CGPoint) -> Int?)? = nil
    /// Called with the toggled spot's id when a marker click lands.
    var onSpotToggled: (@MainActor (Int) -> Void)? = nil

    func makeNSView(context: Context) -> PreviewEventView {
        PreviewEventView(zoom: zoom, spotHitTester: spotHitTester, onSpotToggled: onSpotToggled)
    }

    func updateNSView(_ view: PreviewEventView, context: Context) {
        view.model = zoom
        view.spotHitTester = spotHitTester
        view.onSpotToggled = onSpotToggled
        view.window?.invalidateCursorRects(for: view)
    }
}

/// The `NSView` behind `PreviewEventHost`.
@MainActor
final class PreviewEventView: NSView {
    var model: PreviewZoomModel
    var spotHitTester: (@MainActor (CGPoint) -> Int?)?
    var onSpotToggled: (@MainActor (Int) -> Void)?

    init(
        zoom: PreviewZoomModel,
        spotHitTester: (@MainActor (CGPoint) -> Int?)? = nil,
        onSpotToggled: (@MainActor (Int) -> Void)? = nil
    ) {
        self.model = zoom
        self.spotHitTester = spotHitTester
        self.onSpotToggled = onSpotToggled
        super.init(frame: .zero)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("PreviewEventView is created by PreviewEventHost only")
    }

    /// Match SwiftUI's top-left coordinate space for mouse points.
    override var isFlipped: Bool { true }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window != nil {
            installSpaceMonitor()
        } else {
            uninstallSpaceMonitor()
        }
    }

    override func viewDidUnhide() {
        super.viewDidUnhide()
        window?.invalidateCursorRects(for: self)
    }

    // MARK: - ⌘Space modifier

    private var spaceMonitor: Any?
    private var spaceHeld = false

    /// The uninstall for the monitor above. `viewDidMoveToWindow(nil)` runs
    /// when the representable's view leaves the hierarchy, which covers the
    /// teardown — deinit is nonisolated under Swift 6 and cannot touch
    /// main-actor state, so the monitor is never removed there.
    private func uninstallSpaceMonitor() {
        if let spaceMonitor {
            NSEvent.removeMonitor(spaceMonitor)
        }
        spaceMonitor = nil
        spaceHeld = false
    }

    private static let spaceKeyCode: UInt16 = 49

    /// Tracks the spacebar while this view is on screen so ⌘Space+click can
    /// zoom in from fit and the magnifier cursor can appear. The Edit tab
    /// has no text fields, so space has no other binding to clash with.
    private func installSpaceMonitor() {
        guard spaceMonitor == nil else { return }
        spaceMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.keyDown, .keyUp]
        ) { [weak self] event in
            guard let self, event.keyCode == Self.spaceKeyCode else {
                return event
            }
            self.spaceHeld = event.type == .keyDown
            self.window?.invalidateCursorRects(for: self)
            return event
        }
    }

    private var zoomInModifierHeld: Bool {
        spaceHeld && NSEvent.modifierFlags.contains(.command)
    }

    // MARK: - Mouse

    private var gestureIsActive = false
    private var gestureStart: CGPoint?

    override func mouseDown(with event: NSEvent) {
        if model.mode == .pixels100 {
            gestureIsActive = true
            gestureStart = point(for: event)
            window?.invalidateCursorRects(for: self)
            model.mouseDown(at: point(for: event), kind: .pan)
            return
        }
        if zoomInModifierHeld {
            gestureIsActive = true
            gestureStart = point(for: event)
            window?.invalidateCursorRects(for: self)
            model.mouseDown(at: point(for: event), kind: .zoomInAtClick)
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
        tryToggleSpot(at: point(for: event))
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

    // MARK: - Cursor

    /// Fit + ⌘Space → magnifier (macOS 15+); 100% → open/closed hand;
    /// otherwise the plain arrow.
    override func resetCursorRects() {
        if model.mode == .pixels100 {
            let hand = gestureIsActive ? NSCursor.closedHand : NSCursor.openHand
            addCursorRect(bounds, cursor: hand)
            return
        }
        if zoomInModifierHeld, #available(macOS 15.0, *) {
            addCursorRect(bounds, cursor: NSCursor.zoomIn)
            return
        }
        super.resetCursorRects()
    }

    private func point(for event: NSEvent) -> CGPoint {
        convert(event.locationInWindow, from: nil)
    }
}

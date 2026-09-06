import AppKit
import SwiftUI

/// The Edit tab preview's AppKit event host: tracks the spacebar, swaps the
/// cursor to a magnifier (fit) or a hand (100%), and translates space+click /
/// space+drag into zoom toggles and pans.
///
/// SwiftUI cannot filter a drag on a *held* key the way it filters on
/// `.shift`, so the pan gesture lives on an NSView overlaid on the preview.
/// Plain clicks pass straight through — nothing else on the tab needs them,
/// except the spot markers (SPOTTING_PLAN §8.3): a plain click that hits a
/// marker toggles its rejection, and one that hits nothing keeps doing
/// what it has always done (nothing; space+click zoom lives on the space
/// gesture).
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

    // MARK: - Spacebar

    private var spaceMonitor: Any?

    /// The uninstall for the monitor above. `viewDidMoveToWindow(nil)` runs
    /// when the representable's view leaves the hierarchy, which covers the
    /// teardown — deinit is nonisolated under Swift 6 and cannot touch
    /// main-actor state, so the monitor is never removed there.
    private func uninstallSpaceMonitor() {
        if let spaceMonitor {
            NSEvent.removeMonitor(spaceMonitor)
        }
        spaceMonitor = nil
    }

    private static let spaceKeyCode: UInt16 = 49

    /// Publishes the spacebar state to `model` while this view is on
    /// screen. The Edit tab has no text fields, so space has no other
    /// binding to clash with; the event is never swallowed.
    private func installSpaceMonitor() {
        guard spaceMonitor == nil else { return }
        spaceMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.keyDown, .keyUp]
        ) { [weak self] event in
            guard let self, event.keyCode == Self.spaceKeyCode else {
                return event
            }
            if event.type == .keyDown {
                self.model.spaceDown()
            } else {
                self.model.spaceUp()
            }
            self.window?.invalidateCursorRects(for: self)
            return event
        }
    }

    // MARK: - Mouse

    private var gestureIsActive = false

    override func mouseDown(with event: NSEvent) {
        guard model.spaceHeld else {
            super.mouseDown(with: event)
            return
        }
        gestureIsActive = true
        window?.invalidateCursorRects(for: self)
        model.mouseDown(at: point(for: event), kind: .space)
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
            gestureIsActive = false
            window?.invalidateCursorRects(for: self)
            model.mouseUp(at: point(for: event))
            return
        }
        // A plain click (no space): a marker under the cursor toggles its
        // rejection; empty space keeps doing what it does today (nothing —
        // space+click zoom is the space gesture's).
        if let spotHitTester, let onSpotToggled,
            let spotID = spotHitTester(point(for: event))
        {
            onSpotToggled(spotID)
        }
    }

    // MARK: - Cursor

    /// Fit + space → magnifier (macOS 15+); 100% + space → open/closed hand;
    /// otherwise the plain arrow.
    override func resetCursorRects() {
        guard model.spaceHeld else {
            super.resetCursorRects()
            return
        }
        if model.mode == .fit, #available(macOS 15.0, *) {
            addCursorRect(bounds, cursor: NSCursor.zoomIn)
            return
        }
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

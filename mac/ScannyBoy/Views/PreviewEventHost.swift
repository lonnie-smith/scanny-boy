import AppKit
import SwiftUI

/// The Edit tab preview's AppKit event host: tracks the spacebar, swaps the
/// cursor to a magnifier while it is held, and translates space+click /
/// space+drag into zoom toggles and pans.
///
/// SwiftUI cannot filter a drag on a *held* key the way it filters on
/// `.shift`, so the pan gesture lives on an NSView overlaid on the preview.
/// Plain clicks pass straight through — nothing else on the tab needs them,
/// and the filmstrip's buttons live outside this overlay.
struct PreviewEventHost: NSViewRepresentable {
    /// `PreviewZoomModel` is `@MainActor`, like every `NSView`; the host
    /// only touches it from event callbacks and cursor updates, which all
    /// run on the main thread.
    let zoom: PreviewZoomModel

    func makeNSView(context: Context) -> PreviewEventView {
        PreviewEventView(zoom: zoom)
    }

    func updateNSView(_ view: PreviewEventView, context: Context) {
        view.model = zoom
    }
}

/// The `NSView` behind `PreviewEventHost`.
@MainActor
final class PreviewEventView: NSView {
    var model: PreviewZoomModel

    init(zoom: PreviewZoomModel) {
        self.model = zoom
        super.init(frame: .zero)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("PreviewEventView is created by PreviewEventHost only")
    }

    private var eventMonitor: Any?

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
        model.mouseDown(at: point(for: event))
    }

    override func mouseDragged(with event: NSEvent) {
        guard gestureIsActive else {
            super.mouseDragged(with: event)
            return
        }
        model.mouseDragged(to: point(for: event))
    }

    override func mouseUp(with event: NSEvent) {
        guard gestureIsActive else {
            super.mouseUp(with: event)
            return
        }
        gestureIsActive = false
        model.mouseUp(at: point(for: event))
    }

    // MARK: - Cursor

    /// A magnifier while space is held — zoom-in when the preview is fitted,
    /// zoom-out at 100% — and the plain arrow otherwise. Both magnifier
    /// cursors arrived in macOS 15; on 14 the plain arrow stays.
    override func resetCursorRects() {
        guard model.spaceHeld, #available(macOS 15.0, *) else {
            super.resetCursorRects()
            return
        }
        addCursorRect(
            bounds,
            cursor: model.mode == .fit ? NSCursor.zoomIn : NSCursor.zoomOut
        )
    }

    private func point(for event: NSEvent) -> CGPoint {
        convert(event.locationInWindow, from: nil)
    }
}

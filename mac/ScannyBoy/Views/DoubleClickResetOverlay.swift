import AppKit
import SwiftUI

/// Intercepts firm trackpad clicks and mouse double-clicks on a SwiftUI
/// `Slider`, forwarding everything else to the AppKit slider underneath.
/// Light trackpad taps never arrive as NSEvent mouse clicks, so
/// `doubleClickReset` also attaches a `TapGesture` for those.
struct DoubleClickResetOverlay: NSViewRepresentable {
    let onReset: () -> Void

    func makeNSView(context: Context) -> DoubleClickResetView {
        let view = DoubleClickResetView()
        view.onReset = onReset
        return view
    }

    func updateNSView(_ view: DoubleClickResetView, context: Context) {
        view.onReset = onReset
    }
}

@MainActor
final class DoubleClickResetView: NSView {
    var onReset: (() -> Void)?
    private weak var targetSlider: NSSlider?
    private weak var passThroughTarget: NSView?
    private var isForwarding = false

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func layout() {
        super.layout()
        targetSlider = findSlider()
    }

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            onReset?()
            stopForwarding()
            return
        }
        if let slider = targetSlider ?? findSlider() {
            targetSlider = slider
            passThroughTarget = nil
            slider.mouseDown(with: event)
            isForwarding = true
            return
        }
        passThroughTarget = viewBelow(at: event.locationInWindow)
        passThroughTarget?.mouseDown(with: event)
        isForwarding = passThroughTarget != nil
    }

    override func mouseDragged(with event: NSEvent) {
        guard isForwarding else { return }
        if let slider = targetSlider {
            slider.mouseDragged(with: event)
        } else {
            passThroughTarget?.mouseDragged(with: event)
        }
    }

    override func mouseUp(with event: NSEvent) {
        guard isForwarding else { return }
        if let slider = targetSlider {
            slider.mouseUp(with: event)
        } else {
            passThroughTarget?.mouseUp(with: event)
        }
        stopForwarding()
    }

    private func stopForwarding() {
        isForwarding = false
        passThroughTarget = nil
    }

    private func viewBelow(at locationInWindow: NSPoint) -> NSView? {
        guard let window, let contentView = window.contentView else { return nil }
        isHidden = true
        defer { isHidden = false }
        let point = contentView.convert(locationInWindow, from: nil)
        let hit = contentView.hitTest(point)
        return hit === self ? nil : hit
    }

    private func findSlider() -> NSSlider? {
        let targetFrame = convert(bounds, to: nil)
        var bestMatch: (slider: NSSlider, overlap: CGFloat)?
        var ancestor: NSView? = self
        while let view = ancestor {
            for slider in Self.allSliders(in: view) where !slider.isDescendant(of: self) {
                let sliderFrame = slider.convert(slider.bounds, to: nil)
                let overlap = targetFrame.intersection(sliderFrame)
                let area = overlap.width * overlap.height
                guard area > 0 else { continue }
                if area > (bestMatch?.overlap ?? 0) {
                    bestMatch = (slider, area)
                }
            }
            ancestor = view.superview
        }
        return bestMatch?.slider
    }

    private static func allSliders(in view: NSView) -> [NSSlider] {
        if let slider = view as? NSSlider {
            return [slider]
        }
        return view.subviews.flatMap { allSliders(in: $0) }
    }
}

extension View {
    func doubleClickReset(_ action: @escaping () -> Void) -> some View {
        overlay {
            DoubleClickResetOverlay(onReset: action)
        }
        .simultaneousGesture(
            TapGesture(count: 2).onEnded(action)
        )
    }

    /// Light trackpad double-taps on the Edit preview. Firm clicks are
    /// handled by `PreviewEventView`; light taps never arrive as NSEvent
    /// mouse clicks, so both paths are needed.
    func previewDoubleClickZoom(
        enabled: Bool,
        mode: PreviewZoomModel.Mode,
        onZoomIn: @escaping (CGPoint) -> Void,
        onZoomOut: @escaping () -> Void
    ) -> some View {
        simultaneousGesture(
            SpatialTapGesture(count: 2).onEnded { event in
                guard enabled else { return }
                switch mode {
                case .fit:
                    onZoomIn(event.location)
                case .pixels100:
                    onZoomOut()
                }
            }
        )
    }
}

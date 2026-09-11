import AppKit
import Foundation
import Observation
import SwiftUI

/// Which workspace tab is active — mirrored from `ContentView`'s picker so
/// menu commands can enable and disable themselves.
enum AppWorkspaceTab: Equatable {
    case addScans
    case edit
    case metadata
    case export
}

/// Shared keyboard context for menu-bar shortcuts and the single dispatcher
/// in `ContentView`. Menu commands post notifications; `ContentView`
/// receives them and calls the appropriate model action.
@MainActor
@Observable
final class AppKeyboardState {
    var workspaceTab: AppWorkspaceTab = .addScans
    var isBusy = false

    /// Set by the Edit tab's preview pane while it is mounted.
    var previewHasOutput = false
    var previewOperationsBlocked = false
    /// True while `PreviewPane` is on screen — used to gate menu/keyboard
    /// zoom; never route zoom through a stored closure, which captures a
    /// stale `PreviewPane` value and crashes when invoked from the menu.
    var previewPaneMounted = false
    var zoomToggleCenter = CGPoint.zero
    /// Incremented by `performToggleZoom()`; `PreviewPane` observes this and
    /// toggles its own `@State` zoom model in a live SwiftUI context.
    private(set) var zoomToggleRequest = 0

    var canSelectAll: Bool {
        !isBusy
            && (workspaceTab == .addScans
                || workspaceTab == .edit
                || workspaceTab == .metadata)
    }

    var canDeselectAll: Bool { canSelectAll }

    var canRotate: Bool {
        workspaceTab == .edit
            && !isBusy
            && previewHasOutput
            && !previewOperationsBlocked
    }

    var canZoom: Bool {
        workspaceTab == .edit
            && !isBusy
            && previewHasOutput
            && !previewOperationsBlocked
            && previewPaneMounted
    }

    var canNavigate: Bool {
        (workspaceTab == .edit || workspaceTab == .metadata) && !isBusy
    }

    func performToggleZoom() {
        zoomToggleRequest += 1
    }
}

enum AppKeyboard {
    static let forceNavigateKey = "forceNavigate"

    static func post(_ name: Notification.Name, forceNavigate: Bool = false) {
        NotificationCenter.default.post(
            name: name,
            object: nil,
            userInfo: forceNavigate ? [forceNavigateKey: true] : nil
        )
    }

    static func forceNavigate(from notification: Notification) -> Bool {
        notification.userInfo?[forceNavigateKey] as? Bool ?? false
    }

    /// True when a text field or editor owns first responder — plain ←/→
    /// defer to it; ⌥←/⌥→ navigate anyway.
    @MainActor
    static func isTextInputFirstResponder() -> Bool {
        guard let responder = NSApp.keyWindow?.firstResponder else { return false }
        if responder is NSTextField { return true }
        if let textView = responder as? NSTextView {
            return textView.isFieldEditor || textView.isEditable
        }
        return false
    }
}

extension Notification.Name {
    static let scannyBoySelectAll = Notification.Name(
        "com.lonniesmith.scanny-boy.selectAll"
    )
    static let scannyBoyDeselectAll = Notification.Name(
        "com.lonniesmith.scanny-boy.deselectAll"
    )
    static let scannyBoyRotateCounterClockwise = Notification.Name(
        "com.lonniesmith.scanny-boy.rotateCounterClockwise"
    )
    static let scannyBoyRotateClockwise = Notification.Name(
        "com.lonniesmith.scanny-boy.rotateClockwise"
    )
    static let scannyBoyToggleZoom = Notification.Name(
        "com.lonniesmith.scanny-boy.toggleZoom"
    )
    static let scannyBoySelectPrevious = Notification.Name(
        "com.lonniesmith.scanny-boy.selectPrevious"
    )
    static let scannyBoySelectNext = Notification.Name(
        "com.lonniesmith.scanny-boy.selectNext"
    )
}

/// Menu-bar commands for stage shortcuts. Registered once on the app scene
/// so key equivalents route through `NSMenu`, not SwiftUI view focus.
struct AppKeyboardCommands: Commands {
    @Bindable var keyboard: AppKeyboardState

    var body: some Commands {
        CommandGroup(replacing: .pasteboard) {
            Button("Select All") {
                AppKeyboard.post(.scannyBoySelectAll)
            }
            .keyboardShortcut("a", modifiers: .command)
            .disabled(!keyboard.canSelectAll)

            Button("Deselect All") {
                AppKeyboard.post(.scannyBoyDeselectAll)
            }
            .keyboardShortcut("d", modifiers: .command)
            .disabled(!keyboard.canDeselectAll)

            Divider()

            Button("Rotate Left") {
                AppKeyboard.post(.scannyBoyRotateCounterClockwise)
            }
            .keyboardShortcut("[", modifiers: .command)
            .disabled(!keyboard.canRotate)

            Button("Rotate Right") {
                AppKeyboard.post(.scannyBoyRotateClockwise)
            }
            .keyboardShortcut("]", modifiers: .command)
            .disabled(!keyboard.canRotate)
        }

        CommandMenu("View") {
            Button("Toggle Zoom") {
                AppKeyboard.post(.scannyBoyToggleZoom)
            }
            .keyboardShortcut("z")
            .disabled(!keyboard.canZoom)
        }

        CommandMenu("Navigate") {
            Button("Previous Negative") {
                AppKeyboard.post(.scannyBoySelectPrevious)
            }
            .keyboardShortcut(.leftArrow)
            .disabled(!keyboard.canNavigate)

            Button("Next Negative") {
                AppKeyboard.post(.scannyBoySelectNext)
            }
            .keyboardShortcut(.rightArrow)
            .disabled(!keyboard.canNavigate)

            Divider()

            Button("Previous Negative") {
                AppKeyboard.post(.scannyBoySelectPrevious, forceNavigate: true)
            }
            .keyboardShortcut(.leftArrow, modifiers: .option)
            .disabled(!keyboard.canNavigate)

            Button("Next Negative") {
                AppKeyboard.post(.scannyBoySelectNext, forceNavigate: true)
            }
            .keyboardShortcut(.rightArrow, modifiers: .option)
            .disabled(!keyboard.canNavigate)
        }
    }
}

import AppKit
import Foundation
import Observation
import SwiftUI

/// Which workspace tab is active — mirrored from `ContentView`'s picker so
/// menu commands can enable and disable themselves. Tethered capture and Add
/// Scans are not tabs: they are File-menu sheets (`RollIntakeSheet`).
enum AppWorkspaceTab: Equatable {
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
    var workspaceTab: AppWorkspaceTab = .edit
    var isBusy = false
    /// The roll-intake sheet on screen, if any. While one is up, the
    /// workspace tabs behind it take no shortcuts; only Add Scans' catalogue
    /// answers Select All / Deselect All.
    var intake: RollIntakeKind?
    /// Mirrored from `ContentView`: the sidebar selection names a roll the
    /// library has loaded, so File > Tethered Capture… and Add Scans… have
    /// something to add to.
    var hasSelectedRoll = false

    /// Set by the Edit tab's preview pane while it is mounted.
    var previewHasOutput = false
    var previewOperationsBlocked = false
    /// True while `PreviewPane` is on screen — used to gate menu/keyboard
    /// zoom; never route zoom through a stored closure, which captures a
    /// stale `PreviewPane` value and crashes when invoked from the menu.
    var previewPaneMounted = false
    /// Mirrored from the Edit tab's crop session so ⌘R stays enabled while
    /// cropping (to jump back to the Geometry tab).
    var cropSessionActive = false
    var zoomToggleCenter = CGPoint.zero
    /// Incremented by `performToggleZoom()`; `PreviewPane` observes this and
    /// toggles its own `@State` zoom model in a live SwiftUI context.
    private(set) var zoomToggleRequest = 0

    /// The workspace tabs are frontmost: no intake sheet covers them.
    private var workspaceIsFrontmost: Bool { intake == nil }

    var canOpenIntake: Bool {
        hasSelectedRoll && workspaceIsFrontmost && !isBusy
    }

    var canSelectAll: Bool {
        guard !isBusy else { return false }
        if let intake { return intake == .addScans }
        return workspaceTab == .edit || workspaceTab == .metadata
    }

    var canDeselectAll: Bool { canSelectAll }

    var canRotate: Bool {
        workspaceTab == .edit
            && workspaceIsFrontmost
            && !isBusy
            && previewHasOutput
            && !previewOperationsBlocked
    }

    var canCrop: Bool {
        workspaceTab == .edit
            && workspaceIsFrontmost
            && !isBusy
            && previewHasOutput
            && previewPaneMounted
            && (cropSessionActive || !previewOperationsBlocked)
    }

    var canZoom: Bool {
        workspaceTab == .edit
            && workspaceIsFrontmost
            && !isBusy
            && previewHasOutput
            && !previewOperationsBlocked
            && previewPaneMounted
    }

    var canNavigate: Bool {
        (workspaceTab == .edit || workspaceTab == .metadata) && workspaceIsFrontmost && !isBusy
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

    /// Ends any text editing by resigning the field editor. Blurring a
    /// `CommitTextField` this way commits its edit (to the target the edit
    /// began on), and leaves no text field to claim ⌘A and the arrow keys
    /// — the Metadata tab calls it on arrival and when a filmstrip
    /// thumbnail, the preview or the background is clicked, none of which
    /// take first responder from a focused field on their own.
    @MainActor
    static func endTextEditing() {
        guard isTextInputFirstResponder() else { return }
        _ = NSApp.keyWindow?.makeFirstResponder(nil)
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
    static let scannyBoyBeginCrop = Notification.Name(
        "com.lonniesmith.scanny-boy.beginCrop"
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
            // Replacing `.pasteboard` removes the standard items, and text
            // fields get ⌘X/⌘C/⌘V only through the Edit menu's key
            // equivalents — so put them back, routed up the responder chain.
            Button("Cut") {
                NSApp.sendAction(#selector(NSText.cut(_:)), to: nil, from: nil)
            }
            .keyboardShortcut("x", modifiers: .command)

            Button("Copy") {
                NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: nil)
            }
            .keyboardShortcut("c", modifiers: .command)

            Button("Paste") {
                NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: nil)
            }
            .keyboardShortcut("v", modifiers: .command)

            Divider()

            Button("Select All") {
                // While a text field is being edited ⌘A selects its text, not
                // every negative.
                if AppKeyboard.isTextInputFirstResponder() {
                    NSApp.sendAction(#selector(NSText.selectAll(_:)), to: nil, from: nil)
                } else {
                    AppKeyboard.post(.scannyBoySelectAll)
                }
            }
            .keyboardShortcut("a", modifiers: .command)
            // Do not `.disabled(!keyboard.canSelectAll)` here: a disabled key
            // equivalent swallows ⌘A (and beeps) in a text field on screens
            // where negative selection is unavailable — `ContentView` guards
            // the negative-selection action instead.

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

            Button("Crop") {
                AppKeyboard.post(.scannyBoyBeginCrop)
            }
            .keyboardShortcut("r", modifiers: .command)
            .disabled(!keyboard.canCrop)
        }

        CommandMenu("View") {
            Button("Toggle Zoom") {
                AppKeyboard.post(.scannyBoyToggleZoom)
            }
            .keyboardShortcut("z", modifiers: .command)
            // Do not `.disabled(!keyboard.canZoom)` here: a disabled key
            // equivalent beeps on macOS, and SwiftUI's Commands menu can
            // stay stale until the user clicks — `ContentView` guards the
            // action instead. The preview's local key monitor handles ⌘Z
            // reliably while the Edit tab is visible.
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

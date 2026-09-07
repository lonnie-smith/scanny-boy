import Foundation
import Testing

@testable import ScannyBoy

@Suite("App keyboard state")
@MainActor
struct AppKeyboardStateTests {
    @Test("select-all is available on catalogue and browser tabs when idle")
    func canSelectAll() {
        let keyboard = AppKeyboardState()
        keyboard.isBusy = false

        keyboard.workspaceTab = .addScans
        #expect(keyboard.canSelectAll)

        keyboard.workspaceTab = .edit
        #expect(keyboard.canSelectAll)

        keyboard.workspaceTab = .metadata
        #expect(keyboard.canSelectAll)

        keyboard.workspaceTab = .export
        #expect(!keyboard.canSelectAll)

        keyboard.workspaceTab = .edit
        keyboard.isBusy = true
        #expect(!keyboard.canSelectAll)
    }

    @Test("zoom requires the Edit tab, output, and a registered toggle")
    func canZoom() {
        let keyboard = AppKeyboardState()
        keyboard.workspaceTab = .edit
        keyboard.previewHasOutput = true
        keyboard.toggleZoom = { _ in }

        #expect(keyboard.canZoom)

        keyboard.workspaceTab = .metadata
        #expect(!keyboard.canZoom)

        keyboard.workspaceTab = .edit
        keyboard.toggleZoom = nil
        #expect(!keyboard.canZoom)

        keyboard.toggleZoom = { _ in }
        keyboard.previewOperationsBlocked = true
        #expect(!keyboard.canZoom)
    }

    @Test("navigation is limited to Edit and Metadata while idle")
    func canNavigate() {
        let keyboard = AppKeyboardState()

        keyboard.workspaceTab = .edit
        #expect(keyboard.canNavigate)

        keyboard.workspaceTab = .metadata
        #expect(keyboard.canNavigate)

        keyboard.workspaceTab = .addScans
        #expect(!keyboard.canNavigate)

        keyboard.workspaceTab = .edit
        keyboard.isBusy = true
        #expect(!keyboard.canNavigate)
    }

    @Test("rotate requires Edit tab output and an idle preview")
    func canRotate() {
        let keyboard = AppKeyboardState()
        keyboard.workspaceTab = .edit
        keyboard.previewHasOutput = true

        #expect(keyboard.canRotate)

        keyboard.previewHasOutput = false
        #expect(!keyboard.canRotate)

        keyboard.previewHasOutput = true
        keyboard.previewOperationsBlocked = true
        #expect(!keyboard.canRotate)
    }

    @Test("forceNavigate reads the notification flag")
    func forceNavigateFlag() {
        AppKeyboard.post(.scannyBoySelectPrevious)
        let plain = Notification(name: .scannyBoySelectPrevious)
        #expect(!AppKeyboard.forceNavigate(from: plain))

        AppKeyboard.post(.scannyBoySelectPrevious, forceNavigate: true)
        let forced = Notification(
            name: .scannyBoySelectPrevious,
            userInfo: [AppKeyboard.forceNavigateKey: true]
        )
        #expect(AppKeyboard.forceNavigate(from: forced))
    }
}

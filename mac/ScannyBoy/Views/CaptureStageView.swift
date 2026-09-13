import SwiftUI

/// The Capture workspace tab.
struct CaptureStageView: View {
    @Bindable var capture: CaptureSessionModel
    @Bindable var stitchQueue: StitchQueueModel
    let rig: RigModel
    let grid: GridModel
    let activity: AppActivity
    @FocusState private var captureFocused: Bool

    private let intervalChoices = [2, 3, 4, 5, 6, 8, 10]

    var body: some View {
        VStack(spacing: 0) {
            Form {
                connectionSection
                setupSection
                if capture.flatField == nil {
                    flatFieldReferenceSection
                }
                if capture.filmBase == nil {
                    baseFrameSection
                }
                sequenceSection
            }
            .formStyle(.grouped)
            .disabled(activity.isRollWriteLocked(for: capture.rollURL) && !capture.isSessionOpen)

            if !stitchQueue.negatives.isEmpty {
                CaptureQueueStrip(negatives: stitchQueue.negatives)
                    .padding(.vertical, 8)
            }
        }
        .focusable()
        .focused($captureFocused)
        .onKeyPress(.space) {
            guard captureFocused, !AppKeyboard.isTextInputFirstResponder() else { return .ignored }
            capture.handleSpace()
            return .handled
        }
        .onKeyPress("f") {
            guard captureFocused, !AppKeyboard.isTextInputFirstResponder() else { return .ignored }
            guard capture.isSessionOpen, capture.connectionState == .ready else { return .ignored }
            if capture.focusAssist.isOpen {
                Task { await capture.focusAssist.close() }
            } else if capture.sequencePhase == .idle || capture.sequencePhase == .paused {
                capture.focusAssist.open()
            }
            return .handled
        }
        .onKeyPress("r") {
            guard captureFocused, !AppKeyboard.isTextInputFirstResponder(),
                  capture.focusAssist.isOpen
            else { return .ignored }
            capture.focusAssist.resetPeak()
            return .handled
        }
        .onKeyPress("c") {
            guard captureFocused, !AppKeyboard.isTextInputFirstResponder(),
                  capture.focusAssist.isOpen, !capture.focusAssist.isCheckShotBusy
            else { return .ignored }
            Task { await capture.focusAssist.takeCheckShot(captureFolder: capture.captureFolder) }
            return .handled
        }
        .onKeyPress(.delete) {
            guard captureFocused else { return .ignored }
            capture.handleDelete()
            return .handled
        }
        .onKeyPress(.escape) {
            guard captureFocused else { return .ignored }
            if capture.focusAssist.isOpen {
                Task { await capture.focusAssist.close() }
                return .handled
            }
            capture.handleEscape()
            return .handled
        }
        .onAppear {
            captureFocused = true
            Task { await capture.refreshConnection() }
        }
    }

    @ViewBuilder
    private var connectionSection: some View {
        Section("Camera") {
            Text(connectionMessage)
                .foregroundStyle(connectionStateColor)
            if let exposure = capture.exposure {
                LabeledContent("Program", value: exposure.programDescription)
                LabeledContent("Shutter", value: exposure.shutterDescription)
                LabeledContent("Aperture", value: PTP.decodePropertyValue(0x5007, raw: exposure.aperture))
                LabeledContent("ISO", value: PTP.decodePropertyValue(0x500F, raw: exposure.iso))
            }
            if capture.exposure?.isManualProgram == false {
                Text("Switch the camera to Manual exposure before capturing.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if capture.exposure?.isManualFocus == false {
                Text("Switch the camera to manual focus before capturing.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
    }

    @ViewBuilder
    private var setupSection: some View {
        Section("Setup") {
            Picker("Scanning Rig Profile", selection: $capture.rigProfileID) {
                Text("None").tag(String?.none)
                ForEach(rig.profiles) { profile in
                    Text(profile.name).tag(String?.some(profile.profileID))
                }
            }
            if let warning = capture.apertureMismatchWarning {
                Text(warning)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            Picker("Grid", selection: $capture.gridProfileID) {
                Text("Choose…").tag(String?.none)
                ForEach(grid.profiles) { profile in
                    Text(profile.name).tag(String?.some(profile.profileID))
                }
            }
            if let across = capture.across {
                Text("\(across) × \(capture.down) — \(across * capture.down) scans per negative")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Picker("Interval", selection: $capture.intervalSeconds) {
                ForEach(intervalChoices, id: \.self) { seconds in
                    Text("\(seconds) s").tag(seconds)
                }
            }
            Toggle("Session open", isOn: $capture.sessionOpen)
        }
    }

    @ViewBuilder
    private var flatFieldReferenceSection: some View {
        Section("Bare-light reference") {
            Text(
                "Remove the film and photograph the bare light source alone. "
                    + "Use the same f-stop as your scans; shutter and ISO may differ "
                    + "to avoid clipping."
            )
            .font(.caption)
            .foregroundStyle(.secondary)
            Button("Shoot bare-light reference") {
                Task { await capture.shootFlatFieldReference() }
            }
            .disabled(
                !capture.sessionOpen
                    || capture.connectionState != .ready
                    || capture.isShootingFlatFieldReference
            )
            if capture.isShootingFlatFieldReference {
                ProgressView()
            }
            if let error = capture.flatFieldReferenceError {
                IssueLabel(issue: error, style: .error)
            }
        }
    }

    @ViewBuilder
    private var baseFrameSection: some View {
        Section("Base frame") {
            Button("Shoot base frame") {
                Task { await capture.shootBaseFrame() }
            }
            .disabled(!capture.sessionOpen || capture.connectionState != .ready || capture.isShootingBaseFrame)
            if capture.isShootingBaseFrame {
                ProgressView()
            }
            if let error = capture.baseFrameError {
                IssueLabel(issue: error, style: .error)
            }
        }
    }

    @ViewBuilder
    private var sequenceSection: some View {
        Section("Capture") {
            if capture.focusAssist.isOpen {
                FocusAssistPanel(focusAssist: capture.focusAssist)
            }
            if let across = capture.across, capture.down > 0, !capture.cellStates.isEmpty {
                CaptureMiniView(
                    across: across,
                    down: capture.down,
                    cellStates: capture.cellStates,
                    cellWarnings: capture.cellWarnings
                )
            }
            if !capture.countdownText.isEmpty {
                Text(capture.countdownText)
                    .font(.largeTitle.monospacedDigit())
                    .frame(maxWidth: .infinity)
            }
            Text(
                capture.focusAssist.isOpen
                    ? "F or Esc closes focus assist · R resets peak · C check shot"
                    : "F focus assist · Space starts or pauses · Delete retakes · Esc stops"
            )
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var connectionMessage: String {
        switch capture.connectionState {
        case .absent:
            "Connect the camera over USB and switch it on."
        case .massStorage:
            "The camera is connected as a disk. Set its USB mode to PTP."
        case .unavailable:
            "Another app is using the camera. Quit Photos or Image Capture."
        case .preparing:
            "Preparing the camera — the first connection after plugging in can take a minute."
        case .unsupported:
            "This camera doesn't support tethered capture."
        case .ready:
            "Ready."
        case .busy:
            "Busy."
        case .lost:
            "The camera disconnected. Reconnect to continue."
        }
    }

    private var connectionStateColor: Color {
        switch capture.connectionState {
        case .ready: .primary
        case .busy, .preparing: .secondary
        default: .orange
        }
    }
}

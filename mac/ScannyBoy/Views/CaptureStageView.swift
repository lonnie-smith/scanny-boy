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
                flatFieldReferenceSection
                baseFrameSection
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
        }
    }

    @ViewBuilder
    private var connectionSection: some View {
        Section("Camera") {
            Text(connectionMessage)
                .foregroundStyle(connectionStateColor)
            connectionButtons
            if isConnecting {
                ProgressView()
            }
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
    private var connectionButtons: some View {
        switch capture.connectionState {
        case .absent:
            Button("Connect") {
                Task { await capture.connect() }
            }
        case .massStorage, .unavailable, .unsupported, .lost:
            Button("Retry") {
                Task { await capture.connect() }
            }
        case .searching, .preparing, .ready, .busy:
            Button("Disconnect") {
                Task { await capture.disconnect() }
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
            if let flatField = capture.flatField {
                Label("Captured", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text(flatField.sourceName)
                    .font(.body.monospaced())
                Text("\(flatField.referenceWidth) × \(flatField.referenceHeight)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if flatField.lockedAt != nil {
                    Text("Locked when this roll's first negative was converted.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    captureFlatFieldButton("Capture again")
                }
            } else {
                Text(Self.bareLightInstructions)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                captureFlatFieldButton("Capture bare-light reference")
            }
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
            if let filmBase = capture.filmBase {
                Label("Captured", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text(filmBase.sourceName)
                    .font(.body.monospaced())
                Text(baseFrameDensitySummary(filmBase.density))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let percent = filmBase.areaFractionPercent {
                    Text("rebate: \(percent)% of frame")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if filmBase.lockedAt != nil {
                    Text("Locked when this roll's first negative was converted.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    captureBaseFrameButton("Capture again")
                }
            } else {
                Text(Self.baseFrameInstructions)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                captureBaseFrameButton("Capture base frame")
            }
            if capture.isShootingBaseFrame {
                ProgressView()
            }
            if let error = capture.baseFrameError {
                IssueLabel(issue: error, style: .error)
            }
        }
    }

    private func captureFlatFieldButton(_ title: String) -> some View {
        Button(title) {
            Task { await capture.shootFlatFieldReference() }
        }
        .disabled(
            !capture.sessionOpen
                || capture.connectionState != .ready
                || capture.isShootingFlatFieldReference
                || capture.flatField?.lockedAt != nil
        )
    }

    private func captureBaseFrameButton(_ title: String) -> some View {
        Button(title) {
            Task { await capture.shootBaseFrame() }
        }
        .disabled(
            !capture.sessionOpen
                || capture.connectionState != .ready
                || capture.isShootingBaseFrame
                || capture.filmBase?.lockedAt != nil
        )
    }

    private func baseFrameDensitySummary(_ density: [Double]) -> String {
        density.map { String(format: "%.2f", $0) }.joined(separator: ", ")
    }

    private static let bareLightInstructions = """
        Remove the film and photograph the bare light source alone. Use the \
        same f-stop as your scans; shutter and ISO may differ to avoid clipping.
        """

    private static let baseFrameInstructions = """
        Frame a piece of leader (or any stretch where clear rebate dominates \
        the frame). Keep bare light and sprocket holes out of the frame. \
        Expose at the same shutter, aperture and ISO as your scans, clear \
        of clipping.
        """

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

    private var isConnecting: Bool {
        switch capture.connectionState {
        case .searching, .preparing: true
        default: false
        }
    }

    private var connectionMessage: String {
        switch capture.connectionState {
        case .absent:
            "Plug the camera in over USB, switch it on, then click Connect."
        case .searching:
            "Waiting for the camera…"
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
        case .busy, .preparing, .searching: .secondary
        default: .orange
        }
    }
}

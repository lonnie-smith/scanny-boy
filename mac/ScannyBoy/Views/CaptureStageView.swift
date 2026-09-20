import AppKit
import SwiftUI

/// The Capture workspace tab.
struct CaptureStageView: View {
    @Bindable var capture: CaptureSessionModel
    @Bindable var model: ConfigurationModel
    @Bindable var stitchQueue: StitchQueueModel
    let rig: RigModel
    let grid: GridModel
    let activity: AppActivity
    @FocusState private var captureFocused: Bool
    @State private var isConfirmingDiscard = false

    private let intervalChoices = [2, 3, 4, 5, 6, 8, 10]

    private var isRollLocked: Bool {
        activity.isRollWriteLocked(for: capture.rollURL) && !capture.isSessionOpen
    }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                // Locked per section, not on the Form: disabling the Form
                // also disables its scrolling, stranding the lower sections
                // behind the queue list while the roll is being stitched.
                setupSection.disabled(isRollLocked)
                // Connecting writes nothing to the roll, so it stays usable.
                connectionSection
                flatFieldReferenceSection.disabled(isRollLocked)
                baseFrameSection.disabled(isRollLocked)
                leftoverSection.disabled(isRollLocked)
                sequenceSection.disabled(isRollLocked)
            }
            .formStyle(.grouped)

            if !stitchQueue.negatives(for: capture.rollURL).isEmpty {
                CaptureQueueList(stitchQueue: stitchQueue, rollURL: capture.rollURL)
                    .padding(.vertical, 8)
            }

            discardButton
                .padding(.horizontal)
                .padding(.bottom, 8)
        }
        .confirmationDialog(
            "Discard unpublished captures?",
            isPresented: $isConfirmingDiscard
        ) {
            Button("Discard", role: .destructive) {
                discardUnpublishedCaptures()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(discardConfirmationMessage)
        }
        .focusable()
        .onKeyPress("f") {
            guard captureFocused, !AppKeyboard.isTextInputFirstResponder() else { return .ignored }
            guard capture.isSessionOpen else { return .ignored }
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
        .onChange(of: capture.sequencePhase) { _, _ in
            captureFocused = true
        }
        .onChange(of: capture.gridProfileID) { _, profileID in
            applyCaptureGridSelection(profileID: profileID)
        }
        .onChange(of: capture.intervalSeconds) { _, seconds in
            guard model.rollURL != nil else { return }
            Task { await model.setRollIntervalSeconds(seconds) }
        }
        .onChange(of: grid.profiles) { _, profiles in
            resolveCaptureGridProfile(with: profiles)
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
            .disabled(capture.sequencePhase != .idle)
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
            FilmKindField(
                filmKind: model.filmKind,
                isLocked: model.filmKindLocked,
                isBusy: activity.isBusy,
                error: model.filmKindError,
                unsetHint: "Choose the film type before capturing.",
                onChoose: { choice in
                    Task { await model.setFilmKind(choice.rawValue) }
                }
            )
            Picker("Format", selection: formatBinding) {
                Text("Choose…").tag(FilmFormat?.none)
                ForEach(FilmFormat.allCases) { format in
                    Text(format.label).tag(FilmFormat?.some(format))
                }
            }
        }
    }

    /// The roll's saved film format — a stored choice only, nothing reads
    /// it yet. Choosing a value calls `roll set-setup` immediately.
    private var formatBinding: Binding<FilmFormat?> {
        Binding(
            get: { model.rollFormat },
            set: { newValue in
                guard let newValue else { return }
                Task { await model.setRollFormat(newValue) }
            }
        )
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
            capture.connectionState != .ready
                || capture.isShootingFlatFieldReference
                || capture.flatField?.lockedAt != nil
        )
    }

    private func captureBaseFrameButton(_ title: String) -> some View {
        Button(title) {
            Task { await capture.shootBaseFrame() }
        }
        .disabled(
            capture.connectionState != .ready
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
    private var leftoverSection: some View {
        if capture.hasUnresolvedLeftovers || capture.leftoverError != nil {
            Section("Left in the camera") {
                if !capture.leftoverFrames.isEmpty {
                    Text(Self.leftoverInstructions)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                ForEach(capture.leftoverFrames) { frame in
                    leftoverRow(frame)
                }
                if capture.isClaimingLeftovers {
                    ProgressView("Saving the frame from the camera…")
                }
                if let error = capture.leftoverError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
        }
    }

    private func leftoverRow(_ frame: CaptureSessionModel.LeftoverFrame) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Group {
                if let preview = frame.preview {
                    Image(nsImage: preview.image)
                        .resizable()
                        .scaledToFit()
                } else {
                    Image(systemName: "photo")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 120, height: 90)
            VStack(alignment: .leading, spacing: 6) {
                Text(
                    frame.capturedAt.map {
                        "Shot \($0.formatted(date: .abbreviated, time: .standard))"
                    } ?? "Capture time unknown"
                )
                Text("_unclaimed/\(frame.url.lastPathComponent)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                HStack {
                    if let cell = capture.leftoverTargetCell {
                        Button("Use for cell \(cell + 1)") {
                            capture.useLeftover(frame.id)
                        }
                    }
                    Button("Save to _unclaimed/") {
                        capture.keepLeftover(frame.id)
                    }
                    Button("Discard", role: .destructive) {
                        capture.discardLeftover(frame.id)
                    }
                }
            }
        }
    }

    private static let leftoverInstructions = """
        The camera was holding a frame that was never downloaded — usually the \
        shot a dropped connection cut off. It has been copied to _unclaimed/. \
        Choose what to do with it before capturing.
        """

    @ViewBuilder
    private var sequenceSection: some View {
        Section("Capture") {
            if capture.focusAssist.isOpen {
                FocusAssistPanel(focusAssist: capture.focusAssist)
            }
            Text(sequenceHint)
                .font(.caption)
                .foregroundStyle(.secondary)
            if !capture.focusAssist.isOpen {
                playPauseButton
            }
            if !capture.countdownText.isEmpty {
                Text(capture.countdownText)
                    .font(.largeTitle.monospacedDigit())
                    .frame(maxWidth: .infinity)
            }
            if let across = capture.across, capture.down > 0, !capture.cellStates.isEmpty {
                CaptureMiniView(
                    across: across,
                    down: capture.down,
                    cellStates: capture.cellStates,
                    cellWarnings: capture.cellWarnings
                )
            }
        }
    }

    private var playPauseButton: some View {
        Button {
            capture.handleSpace()
        } label: {
            Label(playPauseTitle, systemImage: playPauseSymbol)
        }
        .buttonStyle(.borderedProminent)
        .keyboardShortcut(.space, modifiers: [])
        .disabled(!capture.canToggleSequence)
        .help(capture.startBlockedReason ?? playPauseTitle)
        .focused($captureFocused)
    }

    private var playPauseTitle: String {
        switch capture.sequencePhase {
        case .idle:
            "Capture next negative"
        case .running:
            "Pause"
        case .paused:
            "Resume"
        case .waitingForDownload:
            "Waiting…"
        }
    }

    private var playPauseSymbol: String {
        switch capture.sequencePhase {
        case .running:
            "pause.fill"
        default:
            "play.fill"
        }
    }

    private var sequenceHint: String {
        if capture.focusAssist.isOpen {
            return "F or Esc closes focus assist · R resets peak · C check shot"
        }
        if capture.hasUnresolvedLeftovers {
            return CaptureSessionModel.leftoverBlockedReason
        }
        if let reason = capture.startBlockedReason {
            return reason
        }
        switch capture.sequencePhase {
        case .idle:
            return "Space or click Capture next negative · F focus assist · Delete retakes · Esc stops"
        case .running:
            return "Space or click Pause · F focus assist · Delete retakes · Esc stops"
        case .paused:
            return "Space or click Resume · F focus assist · Delete retakes · Esc stops"
        case .waitingForDownload:
            return "Waiting for download…"
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

    private var canDiscardUnpublished: Bool {
        capture.hasUnpublishedCaptureWork(
            publishedNegativeIDs: stitchQueue.publishedNegativeIDs
        ) || stitchQueue.hasUnpublishedEntries(for: capture.rollURL)
    }

    private var discardConfirmationMessage: String {
        let unpublishedCount = stitchQueue.negatives(for: capture.rollURL)
            .filter { $0.step != .published }.count
        let inProgressCount = capture.cellStates.filter(\.isFilled).count
        var parts: [String] = []
        if unpublishedCount > 0 {
            parts.append(
                "\(unpublishedCount) queued negative\(unpublishedCount == 1 ? "" : "s")"
            )
        }
        if inProgressCount > 0 {
            parts.append(
                "\(inProgressCount) in-progress frame\(inProgressCount == 1 ? "" : "s")"
            )
        }
        let summary = parts.isEmpty ? "Unpublished capture files" : parts.joined(separator: " and ")
        return "\(summary) will be moved to the Trash. Negatives already published to the roll are kept."
    }

    @ViewBuilder
    private var discardButton: some View {
        Button("Discard unpublished captures…", role: .destructive) {
            isConfirmingDiscard = true
        }
        .disabled(!canDiscardUnpublished || capture.hasUnresolvedLeftovers)
    }

    private func discardUnpublishedCaptures() {
        if capture.sequencePhase != .idle {
            capture.handleEscape()
        }
        guard capture.sequencePhase == .idle, !capture.hasUnresolvedLeftovers else { return }
        let publishedIDs = stitchQueue.publishedNegativeIDs
        var urls = stitchQueue.discardUnpublished(for: capture.rollURL)
        urls.append(contentsOf: capture.discardUnpublishedCaptures(publishedNegativeIDs: publishedIDs))
        let existing = urls.filter { FileManager.default.fileExists(atPath: $0.path) }
        guard !existing.isEmpty else { return }
        NSWorkspace.shared.recycle(existing) { _, _ in }
    }

    private func applyCaptureGridSelection(profileID: String?) {
        if let profileID,
            let profile = grid.profiles.first(where: { $0.profileID == profileID })
        {
            capture.applyGridDimensions(from: profile)
            if model.gridProfileID != profileID {
                model.gridProfileID = profileID
            }
            model.applyGridDimensions(from: profile)
            if model.rollURL != nil {
                Task { await model.setRollGrid(across: profile.across, down: profile.down) }
            }
        } else {
            capture.clearGridSelection()
            if model.gridProfileID != nil {
                model.gridProfileID = nil
            }
            model.across = nil
        }
        reconfigureStitchQueue()
    }

    private func resolveCaptureGridProfile(with profiles: [GridProfile]) {
        guard let profileID = capture.gridProfileID else { return }
        guard let profile = profiles.first(where: { $0.profileID == profileID }) else {
            capture.clearGridSelection()
            return
        }
        capture.applyGridDimensions(from: profile)
    }

    private func reconfigureStitchQueue() {
        guard let rollURL = capture.rollURL,
            let captureFolder = capture.captureFolder,
            let across = capture.across
        else { return }
        stitchQueue.configure(
            roll: rollURL,
            captureFolder: captureFolder,
            rigProfileID: capture.rigProfileID,
            across: across,
            down: capture.down
        )
    }
}

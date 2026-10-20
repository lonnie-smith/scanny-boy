import SwiftUI

/// The two ways a roll gains negatives. Neither is a workspace tab: each is
/// a File-menu command (and a New Roll button) that opens
/// `RollIntakeSheet` on the selected roll.
enum RollIntakeKind: String, Identifiable, Sendable {
    case capture
    case addScans

    var id: String { rawValue }

    var title: String {
        switch self {
        case .capture: "Tethered Capture"
        case .addScans: "Add Scans"
        }
    }

    var systemImage: String {
        switch self {
        case .capture: "camera"
        case .addScans: "photo.on.rectangle.angled"
        }
    }
}

/// One presentation of `RollIntakeSheet`: which stage it shows, pinned to
/// the roll that was selected when it opened. The sheet is modal, so the
/// sidebar selection cannot move away from that roll while it is up.
struct RollIntake: Identifiable {
    let kind: RollIntakeKind
    let roll: Roll

    var id: String { "\(kind.rawValue)|\(roll.id)" }
}

/// Tethered capture or Add Scans, in a sheet over the main window, with the
/// roll it adds to named prominently at the top.
///
/// The stages' Manage… buttons (and the File menu's profile commands) post
/// `.scannyBoyRequestFlatFieldProfiles` / `.scannyBoyRequestGridProfiles`.
/// `ContentView` cannot present those sheets while this one is up, so this
/// sheet answers them itself, nesting the profile sheets on top of it.
struct RollIntakeSheet: View {
    let intake: RollIntake
    let library: RollLibrary
    let rig: RigModel
    let grid: GridModel
    @Bindable var model: ConfigurationModel
    @Bindable var capture: CaptureSessionModel
    @Bindable var stitchQueue: StitchQueueModel
    let edit: EditModel
    let run: RunModel
    let activity: AppActivity

    @Environment(\.dismiss) private var dismiss

    @State private var isPresentingFlatFieldProfiles = false
    @State private var isPresentingGridProfiles = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            stage
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(
            minWidth: 760, idealWidth: 960, maxWidth: .infinity,
            minHeight: 560, idealHeight: 760, maxHeight: .infinity
        )
        // Esc would otherwise close the sheet past `canClose`. Capture never
        // closes on Esc at all: there Esc stops a sequence (§3.2).
        .interactiveDismissDisabled(intake.kind == .capture || !canClose)
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRequestFlatFieldProfiles)
        ) { _ in
            rig.refresh()
            isPresentingFlatFieldProfiles = true
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRequestGridProfiles)
        ) { _ in
            grid.refresh()
            isPresentingGridProfiles = true
        }
        .sheet(isPresented: $isPresentingFlatFieldProfiles) {
            FlatFieldProfilesSheet(rig: rig)
        }
        .sheet(isPresented: $isPresentingGridProfiles) {
            GridProfilesSheet(grid: grid)
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: intake.kind.systemImage)
                .font(.largeTitle)
                .foregroundStyle(.tint)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(intake.kind.title) — adding negatives to")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Text(liveRoll.displayName)
                    .font(.title.bold())
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .accessibilityIdentifier("rollIntakeRollName")
                if let count = liveRoll.negativeCount {
                    Text("Already holds \(Pluralize.count(count, "negative"))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            Button("Done") { dismiss() }
                .keyboardShortcut(doneShortcut)
                .disabled(!canClose)
                .help(closeHelp)
                .accessibilityIdentifier("rollIntakeDoneButton")
        }
        .padding()
        .background(.bar)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(intake.kind.title) for \(liveRoll.displayName)")
    }

    @ViewBuilder
    private var stage: some View {
        switch intake.kind {
        case .capture:
            CaptureStageView(
                capture: capture,
                model: model,
                stitchQueue: stitchQueue,
                rig: rig,
                grid: grid,
                activity: activity
            )
        case .addScans:
            AddScansStageView(
                library: library,
                rig: rig,
                grid: grid,
                model: model,
                edit: edit,
                run: run,
                activity: activity,
                roll: intake.roll
            )
        }
    }

    /// The pinned roll as the library currently reports it, so the name and
    /// negative count follow a rename or a run; falls back to the roll as it
    /// was when the sheet opened (a just-created roll the library has not
    /// rescanned yet).
    private var liveRoll: Roll {
        library.rolls.first { $0.id == intake.roll.id } ?? intake.roll
    }

    /// Esc is a capture key (it stops a sequence, §3.2), so only Add Scans
    /// binds it to Done.
    private var doneShortcut: KeyboardShortcut? {
        intake.kind == .addScans ? .cancelAction : nil
    }

    /// Closing mid-sequence would leave the camera firing with nothing on
    /// screen to pause it, and closing mid-conversion would hide its
    /// progress; both stay open until the work reaches a resting point. A
    /// paused or stopped negative is a resting point: File > Tethered
    /// Capture… reopens it, since the sidebar stays on its roll meanwhile.
    private var canClose: Bool {
        switch intake.kind {
        case .capture:
            !capture.focusAssist.isOpen
                && !isShotInFlight
                && capture.sequencePhase != .running
                && capture.sequencePhase != .waitingForDownload
        case .addScans:
            !activity.isBusy
        }
    }

    private var isShotInFlight: Bool {
        capture.isShootingBaseFrame
            || capture.isShootingFlatFieldReference
            || capture.focusAssist.isCheckShotBusy
    }

    private var closeHelp: String {
        guard !canClose else { return "Close and return to the roll" }
        switch intake.kind {
        case .capture:
            if capture.focusAssist.isOpen { return "Close focus assist first" }
            if isShotInFlight { return "Wait for the shot to finish" }
            return "Pause the sequence first"
        case .addScans:
            return "Wait for the current operation to finish"
        }
    }
}

/// Presents `RollIntakeSheet` for `ContentView` and answers the File menu's
/// Tethered Capture… / Add Scans… commands. A modifier of its own because
/// `ContentView.body`'s chain is already at the type checker's limit.
struct RollIntakePresentation<SheetContent: View>: ViewModifier {
    @Binding var presentedIntake: RollIntake?
    /// Asked to open a sheet on the selected roll; decides whether it may.
    let onRequest: (RollIntakeKind) -> Void
    /// Told which sheet is up after it opens or closes (`nil` once closed).
    let onPresentedChange: (RollIntakeKind?) -> Void
    @ViewBuilder let sheet: (RollIntake) -> SheetContent

    func body(content: Content) -> some View {
        content
            .onReceive(
                NotificationCenter.default.publisher(for: .scannyBoyRequestTetheredCapture)
            ) { _ in
                onRequest(.capture)
            }
            .onReceive(NotificationCenter.default.publisher(for: .scannyBoyRequestAddScans)) { _ in
                onRequest(.addScans)
            }
            .onChange(of: presentedIntake?.kind) { _, kind in
                onPresentedChange(kind)
            }
            .sheet(item: $presentedIntake) { intake in
                sheet(intake)
            }
    }
}

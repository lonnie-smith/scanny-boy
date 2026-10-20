import AppKit
import SwiftUI

/// A `NavigationSplitView` shell — the library sidebar
/// (`RollSidebar`) plus a detail workspace with an Edit/Metadata/Export tab
/// picker. The Edit tab holds negatives, thumbnails, the dirty count, and
/// Apply.
///
/// Negatives come in through `RollIntakeSheet` — Tethered Capture or Add
/// Scans — opened from the File menu, or from `NewRollSheet` right after
/// creating a roll. Either sheet is pinned to the roll selected when it
/// opened; being modal, it keeps the sidebar on that roll until it closes.
///
/// Re-stitch is the same `run` driving
/// `scanny-boy stitch` over a work directory you point at instead of
/// `scanny-boy run` over a fresh selection.
struct ContentView: View {
    let library: RollLibrary
    let rig: RigModel
    let grid: GridModel
    @Bindable var model: ConfigurationModel
    @Bindable var capture: CaptureSessionModel
    @Bindable var stitchQueue: StitchQueueModel
    let edit: EditModel
    let run: RunModel
    let export: ExportModel
    /// The union of every helper session in the app — Convert, rotate,
    /// delete, export, flat-field calibration — since "one helper at a
    /// time" is an app-wide rule, not `RunModel`'s alone.
    let activity: AppActivity
    @Bindable var keyboard: AppKeyboardState

    @State private var selection: Roll.ID?
    @State private var workspaceTab: AppWorkspaceTab = .edit
    /// The Tethered Capture or Add Scans sheet on screen, if any.
    @State private var presentedIntake: RollIntake?
    /// Chosen in `NewRollSheet`; presented from its `onDismiss`, once that
    /// sheet is gone, since a window shows one sheet at a time.
    @State private var pendingIntakeAfterNewRoll: RollIntake?
    @State private var isPresentingRestitch = false
    @State private var restitchWorkDirectory: URL?
    @State private var restitchOutputFolder: URL?
    @State private var isPresentingNewRollSheet = false
    @State private var isPresentingFlatFieldProfiles = false
    @State private var isPresentingGridProfiles = false
    // Left explicit: `.automatic`'s default can collapse to no visible
    // columns at all before the window has a settled size, which leaves
    // both the sidebar and its toolbar absent from the view hierarchy.
    @State private var columnVisibility: NavigationSplitViewVisibility = .all

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            RollSidebar(
                library: library,
                selection: Self.guardedRollSelection(
                    $selection, isBusy: sidebarSelectionLocked
                ),
                runIsActive: sidebarSelectionLocked,
                isPresentingNewRollSheet: $isPresentingNewRollSheet
            )
            .navigationSplitViewColumnWidth(min: 200, ideal: 220)
        } detail: {
            if selection != nil {
                // Attached here rather than on the split view: its modifier
                // chain is already at the type checker's limit.
                workspace
                    .onChange(of: edit.roll?.refreshPending) { _, _ in
                        refreshRollIfPending()
                    }
            } else {
                ContentUnavailableView {
                    Label("No Roll Selected", systemImage: "photo.stack")
                } description: {
                    Text("Choose a roll from the sidebar, or create one.")
                } actions: {
                    Button("New Roll…") {
                        isPresentingNewRollSheet = true
                    }
                    .disabled(activity.isBusy)
                    .accessibilityIdentifier("newRollButtonEmptyState")
                }
            }
        }
        .frame(minWidth: 720, minHeight: 480)
        .versionToolbarItem()
        .onChange(of: selection, initial: true) { _, _ in resolveSelectedRoll() }
        // `selection` can be set (from `NewRollSheet`'s just-created roll)
        // before `library.rolls` has re-scanned to include it — this catches
        // that up rather than leaving `model.rollURL` stuck at `nil`.
        .onChange(of: library.rolls) { _, _ in resolveSelectedRoll() }
        // The run log and the export summary both belong to the roll they
        // ran against: switching rolls clears them, so neither the "Convert
        // Results" section nor the Export tab ever describes the roll that
        // was previously selected. Safe even mid-run — the sidebar blocks
        // switching while anything is active (`AppActivity`) — and both
        // `clearResults` methods guard anyway.
        .onChange(of: model.rollURL) { _, _ in
            run.clearResults()
            export.clearResults()
        }
        .onChange(of: captureRollSetupSyncKey) { _, _ in
            syncCaptureRollSetup()
        }
        .onChange(of: workspaceTab) { _, tab in
            keyboard.workspaceTab = tab
            // Every remaining tab wants the room; the sidebar comes back from
            // the toolbar toggle.
            columnVisibility = .detailOnly
            refreshRollIfPending()
        }
        .onChange(of: activity.isBusy) { _, isBusy in
            keyboard.isBusy = isBusy
        }
        .onAppear(perform: installKeyboardState)
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoySelectAll)) { _ in
            guard keyboard.canSelectAll else { return }
            if let intake = presentedIntake {
                if intake.kind == .addScans { model.selectAll() }
                return
            }
            switch workspaceTab {
            case .edit, .metadata:
                edit.selectAll()
            case .export:
                break
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyDeselectAll)) { _ in
            guard keyboard.canDeselectAll else { return }
            if let intake = presentedIntake {
                if intake.kind == .addScans { model.deselectAll() }
                return
            }
            switch workspaceTab {
            case .edit, .metadata:
                edit.deselectAll()
            case .export:
                break
            }
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRotateCounterClockwise)
        ) { _ in
            guard keyboard.canRotate else { return }
            Task { await edit.rotate(edit.selectionTargets, clockwise: false) }
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRotateClockwise)
        ) { _ in
            guard keyboard.canRotate else { return }
            Task { await edit.rotate(edit.selectionTargets, clockwise: true) }
        }
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyToggleZoom)) { _ in
            guard keyboard.canZoom else { return }
            keyboard.performToggleZoom()
        }
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoySelectPrevious)) {
            notification in
            guard keyboard.canNavigate else { return }
            if !AppKeyboard.forceNavigate(from: notification),
                AppKeyboard.isTextInputFirstResponder()
            {
                return
            }
            edit.selectPrevious()
        }
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoySelectNext)) {
            notification in
            guard keyboard.canNavigate else { return }
            if !AppKeyboard.forceNavigate(from: notification),
                AppKeyboard.isTextInputFirstResponder()
            {
                return
            }
            edit.selectNext()
        }
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyRequestRestitch)) { _ in
            guard presentedIntake == nil else { return }
            restitchWorkDirectory = nil
            restitchOutputFolder = model.rollURL
            isPresentingRestitch = true
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRequestFlatFieldProfiles)
        ) { _ in
            // An open intake sheet answers this itself (`RollIntakeSheet`).
            guard presentedIntake == nil else { return }
            rig.refresh()
            isPresentingFlatFieldProfiles = true
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRequestGridProfiles)
        ) { _ in
            guard presentedIntake == nil else { return }
            grid.refresh()
            isPresentingGridProfiles = true
        }
        .onChange(of: grid.profiles) { _, profiles in
            syncGridProfileSelection(with: profiles)
        }
        .onChange(of: model.gridProfileID) { _, profileID in
            guard let profileID,
                let profile = grid.profiles.first(where: { $0.profileID == profileID })
            else { return }
            model.applyGridDimensions(from: profile)
        }
        .sheet(isPresented: $isPresentingRestitch) {
            RestitchSheet(
                run: run,
                activity: activity,
                rig: rig,
                onStarted: handleRestitchStarted,
                initialWorkDirectory: restitchWorkDirectory,
                initialOutputFolder: restitchOutputFolder
            )
        }
        .sheet(isPresented: $isPresentingNewRollSheet, onDismiss: {
            guard let pending = pendingIntakeAfterNewRoll else { return }
            pendingIntakeAfterNewRoll = nil
            presentedIntake = pending
        }) {
            NewRollSheet(library: library) { roll, intake in
                model.seedRememberedSetup(on: roll.path)
                selection = roll.id
                pendingIntakeAfterNewRoll = RollIntake(kind: intake, roll: roll)
            }
        }
        .sheet(isPresented: $isPresentingFlatFieldProfiles) {
            FlatFieldProfilesSheet(rig: rig)
        }
        .sheet(isPresented: $isPresentingGridProfiles) {
            GridProfilesSheet(grid: grid)
        }
        .modifier(intakePresentation)
    }

    /// Kept out of `body`'s own chain, which is at the type checker's limit.
    private var intakePresentation: some ViewModifier {
        RollIntakePresentation(
            presentedIntake: $presentedIntake,
            onRequest: openIntake,
            onPresentedChange: { kind in
                keyboard.intake = kind
                if kind == nil { refreshRollIfPending() }
            },
            sheet: { intake in
                RollIntakeSheet(
                    intake: intake,
                    library: library,
                    rig: rig,
                    grid: grid,
                    model: model,
                    capture: capture,
                    stitchQueue: stitchQueue,
                    edit: edit,
                    run: run,
                    activity: activity
                )
            }
        )
    }

    /// File > Tethered Capture… / Add Scans…: pins the sheet to the roll
    /// selected right now. Guarded here as well as by the menu items'
    /// enablement, which SwiftUI can leave stale until the menu reopens.
    private func openIntake(_ kind: RollIntakeKind) {
        guard presentedIntake == nil,
            !isPresentingNewRollSheet,
            !isPresentingRestitch,
            !isPresentingFlatFieldProfiles,
            !isPresentingGridProfiles,
            !activity.isBusy,
            let roll = selectedRoll,
            roll.status == .ok,
            model.rollURL == roll.path
        else { return }
        presentedIntake = RollIntake(kind: kind, roll: roll)
    }

    private func syncGridProfileSelection(with profiles: [GridProfile]) {
        guard let profileID = model.gridProfileID else { return }
        guard let profile = profiles.first(where: { $0.profileID == profileID }) else {
            model.gridProfileID = nil
            model.across = nil
            return
        }
        model.applyGridDimensions(from: profile)
    }

    private var workspace: some View {
        VStack(spacing: 0) {
            Picker("Stage", selection: $workspaceTab) {
                Text("Edit").tag(AppWorkspaceTab.edit)
                Text("Metadata").tag(AppWorkspaceTab.metadata)
                Text("Export").tag(AppWorkspaceTab.export)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityLabel("Stage")
            .disabled(activity.isBusy || presentedIntake != nil)
            .padding()

            // Only one stage is ever mounted, as when capture and Add Scans
            // were tabs: the Edit stage's previews must not render behind a
            // capture sheet while its negatives stitch.
            if let intake = presentedIntake {
                ContentUnavailableView(
                    intake.kind.title,
                    systemImage: intake.kind.systemImage,
                    description: Text("Adding negatives to “\(intake.roll.displayName)”.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                stage
            }
        }
        .navigationTitle("Scanny Boy")
        .navigationSubtitle(selectedRollName)
    }

    @ViewBuilder
    private var stage: some View {
        switch workspaceTab {
        case .edit:
            EditStageView(
                edit: edit,
                run: run,
                activity: activity,
                keyboard: keyboard,
                onNegativeDeleted: { library.scan() }
            )
        case .metadata:
            MetadataStageView(
                library: library,
                edit: edit,
                run: run,
                activity: activity
            )
        case .export:
            ExportStageView(export: export, edit: edit, run: run, activity: activity)
        }
    }

    /// The selected roll's name for the window title bar — "Scanny Boy"
    /// first, then the roll, so the user always knows which roll the
    /// workspace is pointed at.
    private var selectedRollName: String {
        selectedRoll?.displayName ?? ""
    }

    private var selectedRoll: Roll? {
        library.rolls.first { $0.id == selection }
    }

    /// Read `capture.holdsRoll` here so sidebar lock state tracks the
    /// capture model directly, not only through `AppActivity`.
    private var sidebarSelectionLocked: Bool {
        capture.holdsRoll || activity.isBusy
    }

    /// Keeps `model.rollURL` and `edit.rollURL` following the sidebar
    /// selection: neither model has a folder picker of its
    /// own, so this is the only thing that ever sets them.
    ///
    /// When `selection` names no roll in `library.rolls`, the roll behind it
    /// is gone (deleted in the Finder, reclassified `unreadable` — which
    /// changes `Roll.id` — or the library base moved in Settings): clear
    /// `selection` too, so the detail pane falls back to "No Roll Selected"
    /// instead of staying mounted on a workspace pointed at nothing. Only
    /// while `library.isScanning == false` — a roll just created by
    /// `NewRollSheet` is legitimately selected before the rescan that will
    /// include it lands, and that transient window must not be mistaken for
    /// a vanished roll.
    private func installKeyboardState() {
        keyboard.workspaceTab = workspaceTab
        keyboard.isBusy = activity.isBusy
        keyboard.intake = presentedIntake?.kind
        installCaptureHandlers()
        installRollDetailHandlers()
        wireCaptureToRoll()
    }

    /// The roll's film stock and format show in the sidebar and in the
    /// Metadata stage's roll copy, so a change made on the Add Scans or
    /// Capture sheet refreshes both; a film stock committed on the Metadata
    /// stage goes the other way, through `ConfigurationModel`'s film-kind
    /// rule, and rescans the sidebar.
    private func installRollDetailHandlers() {
        if model.onRollDetailsChanged == nil {
            model.onRollDetailsChanged = { [edit, library] in
                edit.refresh()
                library.scan()
            }
        }
        if edit.onRollFilmChanged == nil {
            edit.onRollFilmChanged = { [model, edit, library] stock in
                await model.filmStockCommittedElsewhere(stock)
                // The film kind may have followed the stock.
                edit.refresh()
                library.scan()
            }
        }
    }

    private func installCaptureHandlers() {
        if capture.onNegativeCompleted == nil {
            capture.onNegativeCompleted = { [stitchQueue] negative in
                stitchQueue.enqueue(negative)
            }
        }
        if capture.onSessionClosed == nil {
            capture.onSessionClosed = { [stitchQueue] in
                stitchQueue.endSession()
            }
        }
        if capture.onFlatFieldReferenceAttached == nil {
            capture.onFlatFieldReferenceAttached = { [model] flatField in
                model.receiveFlatFieldReference(flatField)
            }
        }
        if capture.onFilmBaseAttached == nil {
            capture.onFilmBaseAttached = { [model] filmBase in
                model.receiveFilmBase(filmBase)
            }
        }
        if stitchQueue.onRollUpdated == nil {
            stitchQueue.onRollUpdated = { [edit, library] in
                edit.refresh()
                library.scan()
            }
        }
        if stitchQueue.onNegativePublished == nil {
            stitchQueue.onNegativePublished = { [edit, library, model] in
                edit.refresh()
                library.scan()
                model.refreshRollLocks()
            }
        }
    }

    /// TETHER_PLAN §4.4: Edit and Export read the highlight lock, so opening
    /// either on a roll whose refresh was deferred runs it first.
    private func refreshRollIfPending() {
        guard presentedIntake == nil,
            workspaceTab == .edit || workspaceTab == .export,
            let rollURL = edit.rollURL,
            edit.roll?.refreshPending == true,
            !activity.isBusy
        else { return }
        Task { await stitchQueue.refreshDeferredRoll(rollURL) }
    }

    private func resolveSelectedRoll() {
        let rollURL = library.rolls.first { $0.id == selection }?.path
        model.rollURL = rollURL
        edit.rollURL = rollURL
        keyboard.hasSelectedRoll = selectedRoll?.status == .ok
        wireCaptureToRoll()
        if selection != nil, rollURL == nil, !library.isScanning {
            selection = nil
        }
    }

    /// Keeps capture's roll-setup fields aligned with Add Scans / roll info.
    /// `rollGrid`/`rollIntervalSeconds` are included because `roll info` is
    /// fetched asynchronously — `wireCaptureToRoll` runs immediately on
    /// selection with whatever `model` last held, and this key's
    /// `onChange` catches the pickers up once the fetch actually lands.
    private var captureRollSetupSyncKey: String {
        Self.captureRollSetupSyncKey(for: model)
    }

    static func captureRollSetupSyncKey(for model: ConfigurationModel) -> String {
        [
            model.filmKind,
            model.filmBase?.sourceName,
            model.filmBase?.lockedAt,
            model.flatField?.sourceName,
            model.flatField?.lockedAt,
            model.rollGrid.map { "\($0.across)x\($0.down)" },
            model.rollIntervalSeconds.map(String.init),
            model.rollFormat?.rawValue,
            String(model.rollAutoCrop),
        ]
        .map { $0 ?? "" }
        .joined(separator: "|")
    }

    private func syncCaptureRollSetup() {
        capture.filmKind = model.filmKind
        capture.filmBase = model.filmBase
        capture.flatField = model.flatField
        // The roll's own saved grid (§ roll set-setup) takes priority over
        // whatever grid was left selected from a previously-open roll —
        // reopening a roll should bring back what was used for it, not
        // carry over a different roll's pick. Dimensions alone can't drive
        // `gridProfileID` (it needs a profile id), so this only applies
        // when a profile with matching dimensions still exists.
        // Not while a negative is being shot: the grid and interval are
        // fixed for its duration, and this runs on every roll rescan.
        guard capture.sequencePhase == .idle else { return }
        if let rollGrid = model.rollGrid,
           let profile = grid.profiles.first(where: {
               $0.across == rollGrid.across && $0.down == rollGrid.down
           })
        {
            capture.gridProfileID = profile.profileID
            capture.applyGridDimensions(from: profile)
        } else if let profileID = capture.gridProfileID,
           let profile = grid.profiles.first(where: { $0.profileID == profileID })
        {
            capture.applyGridDimensions(from: profile)
        } else if capture.gridProfileID == nil {
            capture.gridProfileID = model.gridProfileID
            capture.across = model.across
            capture.down = model.down
        }
        if let rollIntervalSeconds = model.rollIntervalSeconds {
            capture.intervalSeconds = rollIntervalSeconds
        }
    }

    private func wireCaptureToRoll() {
        capture.rollURL = model.rollURL
        syncCaptureRollSetup()
        capture.rigProfileID = model.rigProfileID
        reconfigureStitchQueueIfNeeded()
    }

    private func reconfigureStitchQueueIfNeeded() {
        guard let rollURL = capture.rollURL ?? model.rollURL,
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

    /// Rejects sidebar selection changes while any helper is active (section
    /// 3.10). Testable so the gate stays decoupled from SwiftUI state.
    nonisolated static func guardedRollSelection(
        _ selection: Binding<Roll.ID?>,
        isBusy: Bool
    ) -> Binding<Roll.ID?> {
        Binding(
            get: { selection.wrappedValue },
            set: { newValue in
                guard !isBusy else { return }
                selection.wrappedValue = newValue
            }
        )
    }

    /// Mirrors the Add Scans stage's run tail: a re-stitch can target
    /// `model.rollURL`, so its contents may have changed too.
    private func handleRestitchStarted() {
        awaitRunCompletionAndRefresh()
    }

    /// A run (or re-stitch) rewrites the roll manifest while it works, so
    /// everything that reads the roll back has to re-read it once the run
    /// finishes — not just this tab's validation probe. The Edit tab in
    /// particular may never have been mounted while the run ran, so nothing
    /// else would ever tell `EditModel` the manifest changed: it keeps
    /// showing the pre-run negative list and dirty count.
    private func awaitRunCompletionAndRefresh() {
        Task {
            await run.waitForCompletion()
            model.clearValidationState()
            edit.refresh()
            library.scan()
        }
    }

    /// Section 3.2's "the last folder the user opened" persists across
    /// launches (`ConfigurationModel` stores it in `UserDefaults`), but the
    /// folder itself can vanish in the meantime — an external drive
    /// unmounted, a folder renamed or deleted. Rather than open the panel
    /// somewhere unrelated, walk up the path to the nearest ancestor that
    /// still exists, since that's still meaningfully "close to" where the
    /// user was.
    nonisolated static func closestExistingAncestor(of url: URL?) -> URL? {
        guard var candidate = url else { return nil }
        while !isDirectory(candidate) {
            let parent = candidate.deletingLastPathComponent()
            guard parent != candidate else { return nil }
            candidate = parent
        }
        return candidate
    }

    private nonisolated static func isDirectory(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
        return exists && isDir.boolValue
    }

    /// `canCreateDirectories` is off by default and on only where a new
    /// folder makes sense: an empty folder the user just made cannot be an
    /// input folder, since the CLI would find no NEFs in it.
    ///
    /// Internal, not `private`: `RestitchSheet` reuses this rather than
    /// re-implementing folder picking.
    static func pickFolder(
        startingAt url: URL?,
        message: String? = nil,
        canCreateDirectories: Bool = false
    ) -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = canCreateDirectories
        panel.directoryURL = closestExistingAncestor(of: url)
        if let message {
            panel.message = message
        }
        return panel.runModal() == .OK ? panel.url : nil
    }
}

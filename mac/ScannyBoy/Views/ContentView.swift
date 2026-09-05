import AppKit
import SwiftUI

/// Section 3.10: `NavigationSplitView` shell — the library sidebar
/// (`RollSidebar`) plus a detail workspace with an Add Scans/Edit tab
/// picker. Chunk P3-10 adds the shell and the sidebar. Chunk P3-11 reworks
/// the workspace's Add Scans tab onto the selected roll: no output-folder
/// picker, no film date, and the overwrite-confirmation dialog replaced by
/// the overlap sheet (section 3.4/3.5). Chunk P3-12 adds the Edit tab:
/// negatives, thumbnails, the dirty count, and Apply.
///
/// Scans-per-negative is no longer a roll property at all: it is each
/// stitch batch's own choice, selected on the Add Scans stage and required
/// before the Stitch button enables — so one roll can hold negatives
/// stitched from different scan counts.
///
/// Chunk 9: folder selection, one-range selection, grouping preview.
/// Chunk 10 adds Run with live progress, cooperative Cancel, the
/// completed/failed negatives, Reveal in Finder, and the manifest the run
/// left behind. Chunk P2-10 adds re-stitch: the same `run` driving
/// `scanny-boy stitch` over a work directory you point at instead of
/// `scanny-boy run` over a fresh selection.
struct ContentView: View {
    let library: RollLibrary
    let flatField: FlatFieldModel
    @Bindable var model: ConfigurationModel
    let edit: EditModel
    let run: RunModel
    let export: ExportModel
    /// The union of every helper session in the app — Convert, rotate,
    /// delete, export, flat-field calibration — since "one helper at a
    /// time" (section 3.10) is an app-wide rule, not `RunModel`'s alone.
    let activity: AppActivity

    private enum WorkspaceTab {
        case addScans
        case edit
        case metadata
        case export
    }

    @State private var selection: Roll.ID?
    @State private var workspaceTab: WorkspaceTab = .addScans
    @State private var isPresentingRestitch = false
    @State private var restitchWorkDirectory: URL?
    @State private var restitchOutputFolder: URL?
    @State private var isPresentingNewRollSheet = false
    @State private var isPresentingFlatFieldProfiles = false
    // Left explicit: `.automatic`'s default can collapse to no visible
    // columns at all before the window has a settled size, which leaves
    // both the sidebar and its toolbar absent from the view hierarchy.
    @State private var columnVisibility: NavigationSplitViewVisibility = .all

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            RollSidebar(
                library: library,
                selection: $selection,
                runIsActive: activity.isBusy,
                isPresentingNewRollSheet: $isPresentingNewRollSheet
            )
            .navigationSplitViewColumnWidth(min: 200, ideal: 220)
        } detail: {
            if selection != nil {
                workspace
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
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyRequestRestitch)) { _ in
            restitchWorkDirectory = nil
            restitchOutputFolder = model.rollURL
            isPresentingRestitch = true
        }
        .onReceive(
            NotificationCenter.default.publisher(for: .scannyBoyRequestFlatFieldProfiles)
        ) { _ in
            flatField.refresh()
            isPresentingFlatFieldProfiles = true
        }
        .sheet(isPresented: $isPresentingRestitch) {
            RestitchSheet(
                run: run,
                activity: activity,
                flatField: flatField,
                onStarted: handleRestitchStarted,
                initialWorkDirectory: restitchWorkDirectory,
                initialOutputFolder: restitchOutputFolder
            )
        }
        .sheet(isPresented: $isPresentingNewRollSheet) {
            NewRollSheet(library: library) { roll in
                selection = roll.id
                // A freshly created roll has no scans yet — land on Add Scans
                // so the user can begin adding them, regardless of which tab
                // was active when the sheet opened.
                workspaceTab = .addScans
            }
        }
        .sheet(isPresented: $isPresentingFlatFieldProfiles) {
            FlatFieldProfilesSheet(flatField: flatField)
        }
    }

    private var workspace: some View {
        VStack(spacing: 0) {
            Picker("Stage", selection: $workspaceTab) {
                Text("Add Scans").tag(WorkspaceTab.addScans)
                Text("Edit").tag(WorkspaceTab.edit)
                Text("Metadata").tag(WorkspaceTab.metadata)
                Text("Export").tag(WorkspaceTab.export)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityLabel("Stage")
            .padding()

            switch workspaceTab {
            case .addScans:
                addScansStage
            case .edit:
                EditStageView(
                    edit: edit, run: run, activity: activity,
                    onNegativeDeleted: { library.scan() }
                )
            case .metadata:
                MetadataStageView(
                    library: library, edit: edit, run: run, activity: activity
                )
            case .export:
                ExportStageView(export: export, edit: edit, run: run, activity: activity)
            }
        }
        .navigationTitle("Scanny Boy")
        .navigationSubtitle(selectedRollName)
    }

    /// The selected roll's name for the window title bar — "Scanny Boy"
    /// first, then the roll, so the user always knows which roll the
    /// workspace is pointed at.
    private var selectedRollName: String {
        library.rolls.first { $0.id == selection }?.displayName ?? ""
    }

    private var addScansStage: some View {
        HSplitView {
            catalogueColumn
                .frame(minWidth: 280, idealWidth: 340)
            detailColumn
                .frame(minWidth: 380, idealWidth: 460)
        }
    }

    /// Keeps `model.rollURL` and `edit.rollURL` following the sidebar
    /// selection (section 3.10): neither model has a folder picker of its
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
    private func resolveSelectedRoll() {
        let rollURL = library.rolls.first { $0.id == selection }?.path
        model.rollURL = rollURL
        edit.rollURL = rollURL
        if selection != nil, rollURL == nil, !library.isScanning {
            selection = nil
        }
    }

    private var catalogueColumn: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Input folder").font(.headline)
                Spacer()
                Button("Choose…") { chooseInputFolder() }
            }
            if let inputFolder = model.inputFolder {
                Text(inputFolder.path)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            if model.catalogue.isEmpty {
                ContentUnavailableView(
                    "No NEF Files",
                    systemImage: "photo.on.rectangle.angled",
                    description: Text("Choose a folder of .NEF files to begin.")
                )
            } else {
                List(model.catalogue, id: \.self, selection: $model.selectedFiles) { name in
                    CatalogueRow(name: name, url: model.fileURL(for: name))
                }
                .listStyle(.inset)
            }

            ForEach(model.catalogueWarnings, id: \.self) { warning in
                IssueLabel(issue: warning, style: .warning)
            }
            if let error = model.catalogueError {
                IssueLabel(issue: error, style: .error)
            }
        }
        .padding()
        // Nothing about the configuration may change while a conversion is
        // using it.
        .disabled(activity.isBusy)
    }

    private var detailColumn: some View {
        Form {
            // While a probe is in flight the Stitch button's enablement is
            // not yet trustworthy — but the sections themselves stay
            // mounted (M3): a drag-select across the catalogue used to tear
            // the whole form down and rebuild it once per row, and the
            // grouping picker was unreachable for the duration.
            configurationSections
                .disabled(activity.isBusy)
            runSection
            // Add Scans shows this section for its own invocations only
            // (M9): an apply-metadata started from the Metadata tab is not
            // a conversion, even though it shares the same `RunModel`.
            if run.phase != .idle, run.invocation != .applyMetadata {
                Section("Convert Results") {
                    if run.isActive {
                        RunProgressView(run: run)
                    } else if run.phase == .finishing {
                        FinishingView()
                    } else {
                        RunResultView(run: run)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .padding()
    }

    @ViewBuilder
    private var configurationSections: some View {
        Section("Flat Field") {
            // Chosen fresh for every run: a roll does not lock to one
            // profile, so different runs into the same roll may each pick
            // a different one. Defaults to the last profile used, across
            // any roll.
            Picker("Profile", selection: $model.flatFieldProfileID) {
                Text("None").tag(String?.none)
                ForEach(flatField.profiles) { profile in
                    Text(profile.name).tag(String?.some(profile.profileID))
                }
            }
            if model.flatFieldProfileID == nil {
                Text("Choose the profile measured for this copy stand; it corrects the lens falloff every scan of the roll.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Button("Manage…") {
                flatField.refresh()
                isPresentingFlatFieldProfiles = true
            }
        }
        Section {
            // Scans-per-negative belongs to this stitch batch, not the
            // roll: each run picks its own grouping, so a roll can hold
            // negatives stitched from different scan counts. It must be
            // chosen before stitching — `runEnabled` gates the Stitch
            // button on it, and choosing (or changing) it re-validates the
            // selection, since grouping and divisibility depend on it.
            Picker("Scans per negative", selection: $model.perNegative) {
                Text("Choose scans per negative…").tag(Int?.none)
                ForEach(1...12, id: \.self) { count in
                    Text("\(count)").tag(Int?.some(count))
                }
            }
            .accessibilityIdentifier("perNegativePicker")

            if model.perNegative == nil {
                Text("How many scans are stitched into each negative. Choose one to enable Convert.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("perNegativeHint")
            }
            if !model.groups.isEmpty {
                GroupingPreview(groups: model.groups)
            }
            ForEach(model.selectionWarnings, id: \.self) { warning in
                IssueLabel(issue: warning, style: .warning)
            }
            if let error = model.selectionError {
                IssueLabel(issue: error, style: .error)
            }
            if let error = model.rollError {
                IssueLabel(issue: error, style: .error)
            }
        } header: {
            // A probe in flight only means the Stitch button's enablement
            // is not yet trustworthy (M3) — the section itself, and every
            // control in it, stays put and reachable.
            HStack {
                Text("Grouping")
                if model.isProbing {
                    Spacer()
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
    }

    private var runSection: some View {
        Section {
            HStack {
                Spacer()
                if run.isActive {
                    Button("Cancel", role: .destructive) { run.cancel() }
                        .disabled(!run.canCancel)
                }
                Button("Convert") { startRun() }
                    .disabled(!model.runEnabled || activity.isBusy)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    // A run always adopts whatever it overlaps in place — passing no
    // `--skip-sources` is exactly that: the covered negative keeps its id
    // and filename, and its TIFF is replaced atomically.
    private func startRun() {
        guard let command = model.runCommand(), let rollURL = model.rollURL else { return }
        run.start(
            command: command,
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: rollURL,
            totalNegatives: model.groups.count
        )
        // The roll's contents, and therefore selection validity, change as
        // soon as this finishes.
        awaitRunCompletionAndRefresh()
    }

    /// Mirrors `startRun`'s tail: a re-stitch can target `model.rollURL`,
    /// so its contents may have changed too.
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
            model.refreshValidation()
            edit.refresh()
            library.scan()
        }
    }

    private func chooseInputFolder() {
        guard let url = Self.pickFolder(startingAt: model.inputFolder) else { return }
        model.inputFolder = url
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

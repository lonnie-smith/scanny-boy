import AppKit
import SwiftUI

/// Section 3.10: `NavigationSplitView` shell — the library sidebar
/// (`RollSidebar`) plus a detail workspace with an Add Scans/Edit tab
/// picker. Chunk P3-10 adds the shell and the sidebar. Chunk P3-11 reworks
/// the workspace's Add Scans tab onto the selected roll: no output-folder
/// picker, no film date, the shots-per-negative stepper replaced by the
/// roll's own (read-only here), and the overwrite-confirmation dialog
/// replaced by the overlap sheet (section 3.4/3.5). Chunk P3-12 adds the
/// Edit tab: negatives, thumbnails, the dirty count, and Apply.
///
/// Chunk 9: folder selection, one-range selection, grouping preview.
/// Chunk 10 adds Run with live progress, cooperative Cancel, the
/// completed/failed negatives, Reveal in Finder, and the manifest the run
/// left behind. Chunk P2-10 adds re-stitch: the same `run` driving
/// `scanny-boy stitch` over a work directory you point at instead of
/// `scanny-boy run` over a fresh selection.
struct ContentView: View {
    let library: RollLibrary
    @Bindable var model: ConfigurationModel
    let edit: EditModel
    let run: RunModel

    private enum WorkspaceTab {
        case addScans
        case edit
    }

    @State private var selection: Roll.ID?
    @State private var workspaceTab: WorkspaceTab = .addScans
    @State private var isPresentingRestitch = false
    @State private var restitchWorkDirectory: URL?
    @State private var restitchOutputFolder: URL?
    @State private var isPresentingNewRollSheet = false
    // Left explicit: `.automatic`'s default can collapse to no visible
    // columns at all before the window has a settled size, which leaves
    // both the sidebar and its toolbar absent from the view hierarchy.
    @State private var columnVisibility: NavigationSplitViewVisibility = .all

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            RollSidebar(library: library, selection: $selection, runIsActive: run.isActive)
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
                    .disabled(run.isActive)
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
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyRequestRestitch)) { _ in
            restitchWorkDirectory = nil
            restitchOutputFolder = model.rollURL
            isPresentingRestitch = true
        }
        .sheet(isPresented: $isPresentingRestitch) {
            RestitchSheet(
                run: run,
                onStarted: handleRestitchStarted,
                workDirectory: restitchWorkDirectory,
                outputFolder: restitchOutputFolder
            )
        }
        .sheet(isPresented: $isPresentingNewRollSheet) {
            NewRollSheet(library: library) { roll in
                selection = roll.id
            }
        }
    }

    private var workspace: some View {
        VStack(spacing: 0) {
            Picker("Stage", selection: $workspaceTab) {
                Text("Add Scans").tag(WorkspaceTab.addScans)
                Text("Edit").tag(WorkspaceTab.edit)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding()

            switch workspaceTab {
            case .addScans:
                addScansStage
            case .edit:
                EditStageView(edit: edit, run: run)
            }
        }
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
    private func resolveSelectedRoll() {
        let rollURL = library.rolls.first { $0.id == selection }?.path
        model.rollURL = rollURL
        edit.rollURL = rollURL
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
        .disabled(run.isActive)
    }

    private var detailColumn: some View {
        Form {
            configurationSections
                .disabled(run.isActive)
            runSection
            if run.phase != .idle {
                Section("Run") {
                    if run.isActive {
                        RunProgressView(run: run)
                    } else {
                        RunResultView(run: run, outputFolder: run.outputFolder)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .padding()
    }

    @ViewBuilder
    private var configurationSections: some View {
        Section("Grouping") {
            // Section 3.4: shots-per-negative is the roll's own now, locked
            // once any run reaches complete/partial — Add Scans just shows
            // it. Editable only from the Edit tab (Chunk P3-12), while
            // unlocked.
            Text("Shots per negative: \(model.perNegative)")
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
        }
    }

    private var runSection: some View {
        Section {
            HStack {
                if model.isProbing {
                    ProgressView()
                        .controlSize(.small)
                }
                Spacer()
                if run.isActive {
                    Button("Cancel", role: .destructive) { run.cancel() }
                        .disabled(!run.canCancel)
                }
                Button("Run") { startRun() }
                    .disabled(!model.runEnabled || run.isActive)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    // A run overwrites, in place, any existing negative whose source set
    // exactly matches a new group's — passing no `--skip-sources` is exactly
    // that (CONTRACT.md: "replace is expressed by *not* skipping its sources").
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
        Task {
            await run.waitForCompletion()
            model.refreshValidation()
        }
    }

    /// Mirrors `startRun`'s tail: a re-stitch can target `model.rollURL`,
    /// so its contents may have changed too.
    private func handleRestitchStarted() {
        Task {
            await run.waitForCompletion()
            model.refreshValidation()
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

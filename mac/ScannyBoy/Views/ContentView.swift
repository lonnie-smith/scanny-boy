import AppKit
import SwiftUI

/// Chunk 9: folder selection, one-range selection, grouping preview, film
/// date, output-folder validation, disk estimate, and overwrite-conflict
/// preview. Chunk 10 adds Run with its overwrite confirmation, live progress,
/// cooperative Cancel, the completed/failed negatives, Reveal in Finder, and
/// the manifest the run left behind. Chunk P2-10 adds re-stitch: the same
/// `run` driving `scanny-boy stitch` over a kept work directory instead of
/// `scanny-boy run` over a fresh selection.
struct ContentView: View {
    @Bindable var model: ConfigurationModel
    let run: RunModel

    @State private var isConfirmingOverwrite = false
    @State private var isPresentingRestitch = false
    @State private var restitchWorkDirectory: URL?
    @State private var restitchOutputFolder: URL?

    var body: some View {
        HSplitView {
            catalogueColumn
                .frame(minWidth: 280, idealWidth: 340)
            detailColumn
                .frame(minWidth: 380, idealWidth: 460)
        }
        .frame(minWidth: 720, minHeight: 480)
        .onReceive(NotificationCenter.default.publisher(for: .scannyBoyRequestRestitch)) { _ in
            restitchWorkDirectory = nil
            restitchOutputFolder = model.outputFolder
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
                        RunResultView(
                            run: run, outputFolder: run.outputFolder,
                            onRestitch: presentRestitch
                        )
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
            Stepper(
                "Shots per negative: \(model.perNegative)",
                value: $model.perNegative, in: 1...12
            )
            if !model.groups.isEmpty {
                GroupingPreview(groups: model.groups)
            }
            ForEach(model.selectionWarnings, id: \.self) { warning in
                IssueLabel(issue: warning, style: .warning)
            }
            if let error = model.selectionError {
                IssueLabel(issue: error, style: .error)
            }
        }

        Section("Film date") {
            TextField("YYYY-MM-DD", text: $model.filmDate)
                .textFieldStyle(.roundedBorder)
            if !model.filmDate.isEmpty && !model.isFilmDateValid {
                Text("Enter the date as YYYY-MM-DD.")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }

        Section("Output folder") {
            HStack {
                if let outputFolder = model.outputFolder {
                    Text(outputFolder.path)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                } else {
                    Text("Not chosen").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Choose…") { chooseOutputFolder() }
            }
            if let error = model.outputError {
                IssueLabel(issue: error, style: .error)
            }
            if let required = model.estimatedRequiredBytes, let available = model.availableBytes {
                DiskEstimateView(requiredBytes: required, availableBytes: available)
            }
            if !model.outputConflicts.isEmpty {
                OverwritePreview(
                    conflicts: model.outputConflicts,
                    confirmed: model.overwriteConfirmed
                )
            }
            if let existingRoll = model.existingRoll {
                Text(
                    "This folder already holds a roll (\(existingRoll.status)) "
                        + "from \(existingRoll.filmDate)."
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }

        Section("Intermediates") {
            Toggle("Keep intermediates after a complete run", isOn: $model.keepIntermediates)
                .accessibilityIdentifier("keepIntermediatesToggle")
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
                    .disabled(!model.isReadyPendingOverwriteConfirmation || run.isActive)
                    .keyboardShortcut(.defaultAction)
            }
            // Section 3.6: the exact files that will be replaced, and an
            // explicit agreement, before `--overwrite` is ever passed.
            .confirmationDialog(
                "Replace \(model.outputConflicts.count) existing file(s)?",
                isPresented: $isConfirmingOverwrite
            ) {
                Button("Replace", role: .destructive) {
                    model.confirmOverwrite()
                    beginRun()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(
                    "These files in the output folder will be overwritten:\n"
                        + model.outputConflicts.joined(separator: "\n")
                )
            }
        }
    }

    private func startRun() {
        if model.needsOverwriteConfirmation {
            isConfirmingOverwrite = true
        } else {
            beginRun()
        }
    }

    private func beginRun() {
        guard let command = model.runCommand, let outputFolder = model.outputFolder else {
            return
        }
        run.start(
            command: command,
            files: model.selectedFilesInCanonicalOrder,
            outputFolder: outputFolder
        )
        // The output folder's contents, and therefore the conflict preview and
        // the disk estimate, change as soon as this finishes.
        Task {
            await run.waitForCompletion()
            model.refreshValidation()
        }
    }

    /// Opens the re-stitch sheet, pre-filled with a kept work directory (the
    /// "button" of Chunk P2-10's "a menu command and a button") — `nil` from
    /// the menu command, which has no run to pre-fill from.
    private func presentRestitch(workDirectory: String) {
        restitchWorkDirectory = URL(filePath: workDirectory)
        restitchOutputFolder = model.outputFolder
        isPresentingRestitch = true
    }

    /// Mirrors `beginRun`'s tail: a re-stitch can target `model.outputFolder`
    /// (most often, since the button pre-fills it), so its contents may have
    /// changed too.
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

    private func chooseOutputFolder() {
        // The output folder for a roll of film usually does not exist yet, so
        // the panel offers "New Folder" and starts inside whichever folder
        // was used last (punchlist: "I should be able to create a new folder,
        // not just choose an existing one").
        let url = Self.pickFolder(
            startingAt: model.outputFolder,
            message: "Choose or create the folder to write the TIFFs into.",
            canCreateDirectories: true
        )
        guard let url else { return }
        model.outputFolder = url
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

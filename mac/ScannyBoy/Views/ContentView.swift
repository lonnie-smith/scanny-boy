import AppKit
import SwiftUI

/// Chunk 9: folder selection, one-range selection, grouping preview, film
/// date, output-folder validation, disk estimate, and overwrite-conflict
/// preview. Chunk 10 adds Run with its overwrite confirmation, live progress,
/// cooperative Cancel, the completed/failed negatives, Reveal in Finder, and
/// the manifest the run left behind.
struct ContentView: View {
    @Bindable var model: ConfigurationModel
    let run: RunModel

    @State private var isConfirmingOverwrite = false

    var body: some View {
        HSplitView {
            catalogueColumn
                .frame(minWidth: 280, idealWidth: 340)
            detailColumn
                .frame(minWidth: 380, idealWidth: 460)
        }
        .frame(minWidth: 720, minHeight: 480)
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
                    Text(name)
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
                        RunResultView(run: run, outputFolder: model.outputFolder)
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
                    beginConversion()
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
            beginConversion()
        }
    }

    private func beginConversion() {
        guard let command = model.convertCommand, let outputFolder = model.outputFolder else {
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

    private func chooseInputFolder() {
        guard let url = Self.pickFolder(startingAt: model.inputFolder) else { return }
        model.inputFolder = url
    }

    private func chooseOutputFolder() {
        guard let url = Self.pickFolder(startingAt: model.outputFolder) else { return }
        model.outputFolder = url
    }

    private static func pickFolder(startingAt url: URL?) -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = url
        return panel.runModal() == .OK ? panel.url : nil
    }
}

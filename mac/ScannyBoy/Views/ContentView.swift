import AppKit
import SwiftUI

/// Chunk 9: folder selection, one-range selection, grouping preview, film
/// date, output-folder validation, disk estimate, and overwrite-conflict
/// preview. The Run button is wired to `convert` in Chunk 10 — here it only
/// reflects `model.runEnabled`.
struct ContentView: View {
    @Bindable var model: ConfigurationModel

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
    }

    private var detailColumn: some View {
        Form {
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
                        confirmed: $model.overwriteConfirmed
                    )
                }
            }

            Section {
                HStack {
                    if model.isProbing {
                        ProgressView()
                            .controlSize(.small)
                    }
                    Spacer()
                    Button("Run") {
                        // Chunk 10 wires this to `convert`.
                    }
                    .disabled(!model.runEnabled)
                }
            }
        }
        .formStyle(.grouped)
        .padding()
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

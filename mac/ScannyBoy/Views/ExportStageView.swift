import SwiftUI

/// The Export tab: choose an output folder, then write every negative as a
/// rendered positive in Adobe RGB, written as a lossless JPEG XL (with the
/// negative's recorded tone baked in). The roll's own files are never
/// touched — the CLI replays each negative's ops log over its published
/// pixels, renders, and writes the result into the chosen folder.
struct ExportStageView: View {
    @Bindable var export: ExportModel
    let edit: EditModel
    let run: RunModel
    let activity: AppActivity

    var body: some View {
        Form {
            Section("Output Folder") {
                HStack {
                    Button("Choose…") { chooseOutputFolder() }
                        .disabled(activity.isBusy)
                    Spacer()
                }
                if let directory = export.outputDirectory {
                    Text(directory.path)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }

            Section("Image Size") {
                Picker("Downsampling", selection: $export.downsampleLongEdge) {
                    Text("Full Resolution").tag(Int?.none)
                    Text("6048px (24mp class)").tag(Int?.some(6048))
                    Text("9072px (55mp class)").tag(Int?.some(9072))
                    Text("12096px (100mp class)").tag(Int?.some(12096))
                }
                if let longEdge = export.downsampleLongEdge {
                    Text(
                        "Each export is reduced to \(longEdge) px on its long edge; smaller files, same edits."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                } else {
                    Text("Full resolution is the default; a reduced size exports faster and writes a smaller JPEG XL.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Export") {
                HStack {
                    Text(
                        "Writes each negative as a rendered positive in Adobe RGB, as a lossless JPEG XL. "
                            + "The roll's own files are never modified."
                    )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    Spacer()
                    Button("Export") { startExport() }
                        .disabled(!canExport)
                        .keyboardShortcut(.defaultAction)
                }
                if export.isExporting {
                    ProgressView()
                        .controlSize(.small)
                }
                if let summary = export.completionSummary {
                    Text(summary).font(.caption).foregroundStyle(.secondary)
                }
                ForEach(export.warnings, id: \.self) { warning in
                    Text(warning)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if !export.exportedNegatives.isEmpty {
                Section("Exported") {
                    ForEach(export.exportedNegatives, id: \.negativeID) { negative in
                        HStack {
                            Text(negative.output)
                            Spacer()
                            Text("\(negative.width) × \(negative.height)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .padding()
        .disabled(activity.isBusy)
    }

    private var canExport: Bool {
        guard edit.rollURL != nil, export.outputDirectory != nil else {
            return false
        }
        return export.canExport && edit.roll != nil
    }

    private func chooseOutputFolder() {
        guard let directory = ContentView.pickFolder(
            startingAt: export.outputDirectory,
            message: "Choose where the exported JPEG XLs are written.",
            canCreateDirectories: true
        ) else { return }
        export.outputDirectory = directory
    }

    private func startExport() {
        guard let rollURL = edit.rollURL, let output = export.outputDirectory else {
            return
        }
        export.export(roll: rollURL, output: output)
    }
}

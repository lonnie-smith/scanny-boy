import SwiftUI

/// The Export tab: choose an output folder, then write every negative as a
/// rendered positive in Adobe RGB (with the negative's recorded edits baked
/// in) or as the stitched negative (not inverted, orange mask intact, only
/// rotation, flip and crop applied), as a lossless JPEG XL or a 16-bit
/// TIFF. The roll's own files are never touched — the CLI replays each
/// negative's ops log over its published pixels, renders, and writes the
/// result into the chosen folder.
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

            Section("Image") {
                Picker("Image", selection: $export.mode) {
                    Text("Finished Positive (All Corrections)").tag(CLICommand.ExportMode.positive)
                    Text("Stitched Negative (Crop Only)").tag(CLICommand.ExportMode.negative)
                }
                Text(modeDescription)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Picker("Format", selection: $export.format) {
                    Text("JPEG XL (Lossless)").tag(CLICommand.ExportFormat.jpegXL)
                    Text("TIFF (16-bit)").tag(CLICommand.ExportFormat.tiff)
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
                    Text("Full resolution is the default; a reduced size exports faster and writes a smaller file.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Export") {
                HStack {
                    Text(exportDescription)
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

    private var formatName: String {
        switch export.format {
        case .jpegXL: "a lossless JPEG XL"
        case .tiff: "a 16-bit TIFF"
        }
    }

    private var modeDescription: String {
        switch export.mode {
        case .positive:
            "The rendered positive with every edit applied: tone, colour, repairs and crop."
        case .negative:
            "The stitched scan as the camera saw it, for inverting in another program: "
                + "not inverted, orange mask intact, no tone, colour or repairs. "
                + "Only rotation, flip and crop are applied. Files are named “…-negative”."
        }
    }

    private var exportDescription: String {
        let what = switch export.mode {
        case .positive: "a rendered positive"
        case .negative: "the stitched negative"
        }
        return "Writes each negative as \(what) in Adobe RGB, as \(formatName). "
            + "The roll's own files are never modified."
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
            message: "Choose where the exported files are written.",
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

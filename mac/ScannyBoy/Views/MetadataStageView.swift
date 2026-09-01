import AppKit
import SwiftUI

/// The Metadata tab (protocol version 5): the roll's info and the capture-
/// time Apply control, moved out of the Edit tab when that tab became the
/// filmstrip/preview editor.
///
/// The roll capture date, each negative's date override, and
/// `shots_per_negative` are shown read-only — no CLI command writes them yet
/// (sections 3.5/3.7/3.8 describe what these fields mean but not how the app
/// is meant to set them).
struct MetadataStageView: View {
    @Bindable var edit: EditModel
    let run: RunModel

    var body: some View {
        Form {
            rollSection
            applySection
        }
        .formStyle(.grouped)
        .padding()
        .disabled(run.isActive)
        // `initial: true` matters: a run usually finishes while this tab is
        // not mounted, so the phase can already be `.finished` when the tab
        // first appears — Apply just finished, and the dirty count and
        // applied timestamps it changed are only known once the roll is
        // re-read.
        .onChange(of: run.phase, initial: true) { _, phase in
            if phase == .finished { edit.refresh() }
        }
    }

    private var rollSection: some View {
        Section("Roll") {
            if let roll = edit.roll {
                LabeledContent("Name", value: roll.rollName)
                LabeledContent("Shots per negative", value: String(roll.shotsPerNegative))
                LabeledContent("Capture date", value: roll.metadata.rollCaptureDate ?? "Not set")
            }
            if let rollURL = edit.rollURL {
                LabeledContent("Folder") {
                    HStack {
                        Text(rollURL.path)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button("Open") {
                            NSWorkspace.shared.activateFileViewerSelecting([rollURL])
                        }
                    }
                }
            }
        }
    }

    private var applySection: some View {
        Section("Capture Time") {
            HStack {
                Text("\(edit.dirtyCount) negative(s) need their capture time written")
                Spacer()
                Button("Apply") { applyMetadata() }
                    .disabled(!edit.canApply || run.isActive)
            }
            if run.phase == .finished, let summary = run.completionSummary,
                run.stitchedNegatives.isEmpty, !run.appliedNegativeIDs.isEmpty
                    || !run.skippedMetadata.isEmpty
            {
                Text(summary).font(.caption).foregroundStyle(.secondary)
                ForEach(run.skippedMetadata, id: \.groupID) { skipped in
                    IssueLabel(
                        issue: ConfigurationModel.Issue(code: skipped.code, message: skipped.message),
                        style: .warning
                    )
                }
            }
        }
    }

    private func applyMetadata() {
        guard let command = edit.applyCommand, let rollURL = edit.rollURL else { return }
        run.start(
            command: command, files: [], outputFolder: rollURL, totalNegatives: edit.dirtyCount
        )
        Task {
            await run.waitForCompletion()
            edit.refresh()
        }
    }
}

import AppKit
import SwiftUI

/// The Metadata tab (protocol version 5): the roll's info and the capture-
/// time Apply control, moved out of the Edit tab when that tab became the
/// filmstrip/preview editor.
///
/// The roll capture date and each negative's date override are shown
/// read-only — no CLI command writes them yet (sections 3.5/3.7/3.8
/// describe what these fields mean but not how the app is meant to set
/// them). The roll has no shots-per-negative to show: grouping is each
/// stitch batch's own choice.
struct MetadataStageView: View {
    @Bindable var edit: EditModel
    let run: RunModel
    let activity: AppActivity

    var body: some View {
        Form {
            rollSection
            applySection
        }
        .formStyle(.grouped)
        .padding()
        .disabled(activity.isBusy)
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
                    .disabled(!edit.canApply || activity.isBusy)
            }
            // Keyed on the actual invocation (M9), not inferred from the
            // shape of the results — that heuristic missed an apply that
            // failed outright (zero applied, zero skipped) and an apply of
            // zero negatives, both indistinguishable by shape from "no
            // apply ever ran".
            if run.phase == .finished, run.invocation == .applyMetadata,
                let summary = run.completionSummary
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

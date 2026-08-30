import SwiftUI

/// Chunk P2-10's re-stitch action: takes a kept work directory and an output
/// folder and runs `stitch`, reusing `run` — the very `RunModel` a normal Run
/// drives — so progress and results show up in the same UI.
/// (`docs/PHASE2_IMPLEMENTATION_PLAN.md`, "App: re-stitch".) This is what
/// makes tuning cost minutes rather than hours: the work directory survives a
/// run precisely so it can be handed back here.
///
/// Section 3.6 asks for "the exact files that will be replaced, and an
/// explicit agreement, before `--overwrite` is ever passed" — the rule
/// `ContentView`'s own overwrite-confirmation dialog follows for `run`. That
/// exact list comes from `probe --out`, which (per Chunk P2-9's
/// `ConfigurationModel.existingRoll`) has no notion of `scanny-boy-roll.json`
/// at all, so it cannot enumerate what a re-stitch would replace. This sheet
/// asks for one general, explicit agreement instead of an itemized one, and
/// passes `--overwrite` unconditionally once it is given — a no-op if the
/// output folder turns out to hold nothing that conflicts. Real enforcement,
/// as everywhere else in this app, happens for real, server-side, in
/// `run_stitch`.
struct RestitchSheet: View {
    let run: RunModel
    /// Called immediately after `run.start(...)`, so the caller can wait for
    /// completion and refresh whatever else depends on the output folder —
    /// the same thing `ContentView` does after a normal Run.
    let onStarted: () -> Void

    @Environment(\.dismiss) private var dismiss

    @State var workDirectory: URL?
    @State var outputFolder: URL?
    @State private var overwriteAcknowledged = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Re-stitch").font(.title2.bold())
            Text("Re-run the stitch stage over a kept work directory's intermediates, without decoding the RAW files again.")
                .font(.callout)
                .foregroundStyle(.secondary)

            folderRow(
                title: "Work directory", folder: workDirectory,
                message: "Choose the kept work directory to re-stitch."
            ) { workDirectory = $0 }

            folderRow(
                title: "Output folder", folder: outputFolder,
                message: "Choose the folder to write the stitched TIFFs into.",
                canCreateDirectories: true
            ) { outputFolder = $0 }

            Toggle(
                "This may replace an existing roll in the output folder.",
                isOn: $overwriteAcknowledged
            )

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Stitch") { startRestitch() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!isReady)
            }
        }
        .padding(20)
        .frame(minWidth: 480)
    }

    private var isReady: Bool {
        workDirectory != nil && outputFolder != nil && overwriteAcknowledged && !run.isActive
    }

    @ViewBuilder
    private func folderRow(
        title: String,
        folder: URL?,
        message: String,
        canCreateDirectories: Bool = false,
        onChoose: @escaping (URL) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.headline)
            HStack {
                if let folder {
                    Text(folder.path)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                } else {
                    Text("Not chosen").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Choose…") {
                    guard
                        let url = ContentView.pickFolder(
                            startingAt: folder, message: message,
                            canCreateDirectories: canCreateDirectories
                        )
                    else { return }
                    onChoose(url)
                }
            }
        }
    }

    private func startRestitch() {
        guard let workDirectory, let outputFolder else { return }
        run.start(
            command: .stitch(work: workDirectory, out: outputFolder, overwrite: true),
            files: [],
            outputFolder: outputFolder
        )
        onStarted()
        dismiss()
    }
}

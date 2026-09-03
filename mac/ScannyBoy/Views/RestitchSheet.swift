import SwiftUI

/// Chunk P2-10's re-stitch action: takes a work directory and an output
/// folder and runs `stitch`, reusing `run` — the very `RunModel` a normal Run
/// drives — so progress and results show up in the same UI.
/// (`docs/PHASE2_IMPLEMENTATION_PLAN.md`, "App: re-stitch".) This is what
/// makes tuning cost minutes rather than hours: a work directory you keep
/// (a run started with `--work`, since a run never keeps the one it creates
/// itself) can be handed back here.
///
/// Section 3.6 asks for "the exact files that will be replaced, and an
/// explicit agreement, before `--overwrite` is ever passed" — the rule
/// `run`'s own overlap sheet follows (section 3.4/3.5). A re-stitch has no
/// selection to probe for overlap at all, since it re-runs the stitch stage
/// over intermediates already on disk rather than a fresh input selection.
/// This sheet asks for one general, explicit agreement instead of an
/// itemized one, and
/// passes `--overwrite` unconditionally once it is given — a no-op if the
/// output folder turns out to hold nothing that conflicts. Real enforcement,
/// as everywhere else in this app, happens for real, server-side, in
/// `run_stitch`.
struct RestitchSheet: View {
    let run: RunModel
    /// Re-stitch drives `run` directly, so `RunModel.start`'s own
    /// `guard !isActive` would refuse a second *run*, but not a run started
    /// while a rotate, delete, export, or flat-field calibration has its own
    /// helper busy (P4) — this menu-triggered sheet has no other gate on it.
    let activity: AppActivity
    /// The profile list, for the optional `--flatfield` picker: a roll whose
    /// first stitch ran with a calibrated profile has its geometry locked
    /// into its invariants, and a re-stitch without the same profile is
    /// refused (`ROLL_INVARIANT_MISMATCH`).
    let flatField: FlatFieldModel
    /// Called immediately after `run.start(...)`, so the caller can wait for
    /// completion and refresh whatever else depends on the output folder —
    /// the same thing `ContentView` does after a normal Run.
    let onStarted: () -> Void

    /// Seeds for `workDirectory`/`outputFolder` below, copied across in
    /// `.onAppear` (M7). SwiftUI applies a `@State` property's memberwise-
    /// init value only the *first* time a view identity appears — plain
    /// `@State var workDirectory: URL? = workDirectory` happened to work
    /// only because `.sheet(isPresented:)` tears this view down on every
    /// dismiss, so a second presentation is always a fresh identity. Kept
    /// as `let` so nothing can silently rely on that coincidence again.
    let initialWorkDirectory: URL?
    let initialOutputFolder: URL?

    @Environment(\.dismiss) private var dismiss

    @State private var workDirectory: URL?
    @State private var outputFolder: URL?
    @State private var overwriteAcknowledged = false
    @State private var flatFieldProfileID: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Re-stitch").font(.title2.bold())
            Text("Re-run the stitch stage over a work directory's intermediates, without decoding the RAW files again.")
                .font(.callout)
                .foregroundStyle(.secondary)

            folderRow(
                title: "Work directory", folder: workDirectory,
                message: "Choose the work directory to re-stitch."
            ) { workDirectory = $0 }

            folderRow(
                title: "Output folder", folder: outputFolder,
                message: "Choose the folder to write the stitched TIFFs into.",
                canCreateDirectories: true
            ) { outputFolder = $0 }

            Picker("Calibration profile", selection: $flatFieldProfileID) {
                Text("None").tag(String?.none)
                ForEach(flatField.profiles) { profile in
                    Text(profile.name).tag(String?.some(profile.profileID))
                }
            }

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
        .onAppear {
            workDirectory = initialWorkDirectory
            outputFolder = initialOutputFolder
        }
    }

    private var isReady: Bool {
        workDirectory != nil && outputFolder != nil && overwriteAcknowledged && !activity.isBusy
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
        let flatFieldProfileID = flatFieldProfileID
        // Resolved off-main (M7) before `run.start`, exactly as
        // `RunModel.readManifest` reads its own manifest off-main: it is
        // small file I/O, but still file I/O on the path that starts a run.
        Task {
            let totalNegatives = await Self.totalNegatives(inWorkDirectory: workDirectory)
            run.start(
                command: .stitch(
                    work: workDirectory, roll: outputFolder, overwrite: true,
                    flatfield: flatFieldProfileID
                ),
                files: [],
                outputFolder: outputFolder,
                totalNegatives: totalNegatives
            )
            onStarted()
        }
        dismiss()
    }

    /// The work directory always holds `scanny-boy-manifest.json`, the
    /// convert stage's own record (CONTRACT.md) — the only place a negative
    /// count for this re-stitch is known ahead of time.
    private static func totalNegatives(inWorkDirectory workDirectory: URL) async -> Int? {
        await Task.detached {
            try? RunManifest.read(inOutputFolder: workDirectory).groups.count
        }.value
    }
}

import SwiftUI

/// The library sidebar (section 3.10): rolls sorted by name, a **+** to
/// create one, and a context menu with Rename and Delete. Its whole content
/// comes from `RollLibrary`'s one `roll list` scan — no filesystem
/// enumeration of its own.
struct RollSidebar: View {
    let library: RollLibrary
    @Binding var selection: Roll.ID?
    /// Disables every mutating action while a run is active app-wide
    /// (section 3.10).
    let runIsActive: Bool
    /// `ContentView` owns `NewRollSheet`'s presentation (L5): the sidebar's
    /// toolbar **+** and the empty-state's "New Roll…" button used to each
    /// present their own instance with their own post-create behaviour,
    /// which happened to agree today but had no reason to stay that way.
    /// This binding is the sidebar's only way to ask for the sheet.
    @Binding var isPresentingNewRollSheet: Bool

    @State private var renamingRoll: Roll?
    @State private var renameText = ""
    @State private var deletingRoll: Roll?
    @State private var errorMessage: String?

    private var sortedRolls: [Roll] {
        library.rolls.sorted { $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending }
    }

    var body: some View {
        List(selection: $selection) {
            ForEach(sortedRolls) { roll in
                RollRow(roll: roll)
                    .tag(roll.id)
                    .selectionDisabled(roll.status == .unreadable)
                    .contextMenu {
                        Button("Rename…") { beginRename(roll) }
                            .disabled(roll.status != .ok || runIsActive)
                        Button("Delete…", role: .destructive) { deletingRoll = roll }
                            .disabled(runIsActive)
                    }
            }
        }
        .overlay {
            if library.isScanning, sortedRolls.isEmpty {
                VStack(spacing: 8) {
                    ProgressView()
                    Text("Loading rolls…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Rolls")
        .safeAreaInset(edge: .bottom) {
            // A failed scan leaves `rolls` at its last successful snapshot —
            // without this, the list silently shows stale counts (a roll
            // stitched since reads as it was before) instead of saying why
            // nothing refreshed. Pinned below the list rather than shown as a
            // list row: sidebar-styled lists force single-line rows, so a row
            // would truncate the message instead of wrapping it.
            if let scanError = library.scanError {
                HStack(alignment: .firstTextBaseline) {
                    Image(systemName: "exclamationmark.triangle")
                    Text("The library could not be read: \(scanError)")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.background.secondary)
            }
        }
        .toolbar {
            ToolbarItem {
                HStack(spacing: 8) {
                    if library.isScanning, !sortedRolls.isEmpty {
                        ProgressView()
                            .controlSize(.small)
                    }
                    Button {
                        isPresentingNewRollSheet = true
                    } label: {
                        Label("New Roll", systemImage: "plus")
                    }
                    .disabled(runIsActive)
                    .accessibilityIdentifier("newRollButton")
                }
            }
        }
        .task { library.scan() }
        .sheet(item: $renamingRoll) { roll in
            renameSheet(for: roll)
        }
        .confirmationDialog(
            "Move “\(deletingRoll?.displayName ?? "")” to the Trash?",
            isPresented: Binding(
                get: { deletingRoll != nil },
                set: { if !$0 { deletingRoll = nil } }
            )
        ) {
            Button("Move to Trash", role: .destructive) { confirmDelete() }
            Button("Cancel", role: .cancel) {}
        } message: {
            if let count = deletingRoll?.negativeCount {
                Text("This roll holds \(count) published TIFF(s). This cannot be undone.")
            }
        }
        .alert(
            "Something went wrong",
            isPresented: Binding(get: { errorMessage != nil }, set: { if !$0 { errorMessage = nil } })
        ) {
            Button("OK") { errorMessage = nil }
        } message: {
            Text(errorMessage ?? "")
        }
    }

    private func beginRename(_ roll: Roll) {
        renameText = roll.displayName
        renamingRoll = roll
    }

    @ViewBuilder
    private func renameSheet(for roll: Roll) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename Roll").font(.title2.bold())
            TextField("Name", text: $renameText)
                .textFieldStyle(.roundedBorder)
                .onSubmit { confirmRename(roll) }
            HStack {
                Spacer()
                Button("Cancel") { renamingRoll = nil }
                Button("Rename") { confirmRename(roll) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(renameText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(20)
        .frame(minWidth: 360)
    }

    private func confirmRename(_ roll: Roll) {
        let newName = renameText
        renamingRoll = nil
        Task {
            do {
                let renamed = try await library.renameRoll(roll, to: newName, runIsActive: runIsActive)
                selection = renamed.id
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func confirmDelete() {
        guard let roll = deletingRoll else { return }
        deletingRoll = nil
        Task {
            do {
                try await library.deleteRoll(roll)
                if selection == roll.id { selection = nil }
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}

private struct RollRow: View {
    let roll: Roll

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(roll.displayName)
                if roll.status == .ok, let count = roll.negativeCount {
                    Text("\(count) negative(s)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if let reason = roll.reason {
                    Text(reason.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if roll.status == .unreadable {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
        }
        .disabled(roll.status == .unreadable)
    }
}

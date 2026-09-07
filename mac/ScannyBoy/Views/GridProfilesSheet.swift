import SwiftUI

/// The grid configuration manager: the preset list, each row with a trash
/// button, plus New Configuration… — a name field, grid-size pickers, and
/// Create. Modeled on `FlatFieldProfilesSheet`.
struct GridProfilesSheet: View {
    let grid: GridModel

    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var across = 3
    @State private var down = 1
    @State private var createError: String?
    @State private var deleteError: String?
    @State private var profilePendingDeletion: GridProfile?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Manage Grid Configurations").font(.title2.bold())

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    profileList

                    if let deleteError {
                        Text(deleteError)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }

                    Divider()

                    newProfile
                }
            }

            HStack {
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(hasNewProfileContent ? .cancelAction : .defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 420, minHeight: 280, maxHeight: 520)
        .confirmationDialog(
            "Delete “\(profilePendingDeletion?.name ?? "")”?",
            isPresented: Binding(
                get: { profilePendingDeletion != nil },
                set: { if !$0 { profilePendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete Configuration", role: .destructive) {
                if let profile = profilePendingDeletion {
                    Task { await delete(profile) }
                }
                profilePendingDeletion = nil
            }
            Button("Cancel", role: .cancel) { profilePendingDeletion = nil }
        } message: {
            Text("This cannot be undone.")
        }
    }

    @ViewBuilder
    private var profileList: some View {
        if grid.profiles.isEmpty {
            Text("No configurations yet.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            VStack(spacing: 0) {
                ForEach(grid.profiles) { profile in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(profile.name)
                            Text("\(profile.dimensionSummary) — \(profile.scanCount) scans per negative")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            profilePendingDeletion = profile
                        } label: {
                            Image(systemName: "trash")
                                .frame(width: 24, height: 24)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("Delete \(profile.name)")
                    }
                    .padding(.vertical, 4)
                }
            }
        }
    }

    private var newProfile: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("New Configuration").font(.headline)

            VStack(alignment: .leading, spacing: 4) {
                Text("Name")
                TextField("", text: $name)
                    .textFieldStyle(.roundedBorder)
            }

            LabeledContent("Grid size") {
                HStack(spacing: 8) {
                    Picker("", selection: $across) {
                        ForEach(1...(ConfigurationModel.maxPerNegative / down), id: \.self) { count in
                            Text("\(count)").tag(count)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 72)

                    Text("×")
                        .foregroundStyle(.secondary)

                    Picker("", selection: $down) {
                        ForEach(1...2, id: \.self) { count in
                            Text("\(count)").tag(count)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 72)
                    .onChange(of: down) { _, newDown in
                        if across * newDown > ConfigurationModel.maxPerNegative {
                            across = ConfigurationModel.maxPerNegative / newDown
                        }
                    }
                }
            }

            Text("\(across * down) scans per negative")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let createError {
                Text(createError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                Spacer()
                createButton
            }
        }
    }

    @ViewBuilder
    private var createButton: some View {
        let button = Button("Create") { create() }
            .disabled(!isReady)
        if hasNewProfileContent {
            button
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        } else {
            button
        }
    }

    private var hasNewProfileContent: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var isReady: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func create() {
        createError = nil
        Task {
            let result = await grid.create(name: name, across: across, down: down)
            switch result {
            case .success:
                name = ""
            case .failure(_, let message):
                createError = message
            }
        }
    }

    private func delete(_ profile: GridProfile) async {
        deleteError = nil
        let result = await grid.delete(profile)
        if case .failure(_, let message) = result {
            deleteError = message
        }
    }
}

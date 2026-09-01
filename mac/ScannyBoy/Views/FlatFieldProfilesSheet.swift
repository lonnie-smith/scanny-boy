import SwiftUI
import UniformTypeIdentifiers

/// The flat-field profile manager: the profile list, each row with a trash
/// button (confirming — a deleted gain map cannot be recovered, and the
/// CLI's `FLATFIELD_PROFILE_IN_USE` refusal for profiles locked into a
/// roll's invariants arrives here as an alert), plus New Profile… — an
/// `NSOpenPanel` limited to NEF, a name field, and Create with a spinner,
/// since building a profile decodes a RAW and takes seconds. Modeled on
/// `NewRollSheet`.
struct FlatFieldProfilesSheet: View {
    let flatField: FlatFieldModel

    @Environment(\.dismiss) private var dismiss

    @State private var referenceURL: URL?
    @State private var name = ""
    @State private var isCreating = false
    @State private var createError: String?
    @State private var deleteError: String?
    @State private var profilePendingDeletion: FlatFieldProfile?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Flat-Field Profiles").font(.title2.bold())
            Text(
                "A profile measures one copy stand's falloff from a shot of the bare light source — no negative in the holder — and evens it back out of every scan."
            )
            .font(.caption)
            .foregroundStyle(.secondary)

            profileList

            Divider()

            newProfile

            if let deleteError {
                Text(deleteError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(minWidth: 460, minHeight: 320)
        .confirmationDialog(
            "Delete “\(profilePendingDeletion?.name ?? "")”?",
            isPresented: Binding(
                get: { profilePendingDeletion != nil },
                set: { if !$0 { profilePendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete Profile", role: .destructive) {
                if let profile = profilePendingDeletion {
                    Task { await delete(profile) }
                }
                profilePendingDeletion = nil
            }
            Button("Cancel", role: .cancel) { profilePendingDeletion = nil }
        } message: {
            Text("This cannot be undone. A profile locked into a roll cannot be deleted.")
        }
    }

    @ViewBuilder
    private var profileList: some View {
        if flatField.profiles.isEmpty {
            Text("No profiles yet.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            List(flatField.profiles) { profile in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(profile.name)
                        if let width = profile.referenceWidth,
                            let height = profile.referenceHeight
                        {
                            Text("Reference \(width) × \(height)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    Button {
                        profilePendingDeletion = profile
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Delete \(profile.name)")
                }
            }
            .listStyle(.inset)
            .frame(minHeight: 80)
        }
    }

    private var newProfile: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("New Profile").font(.headline)

            HStack {
                Button("Choose Reference…") { chooseReference() }
                Text(referenceURL?.lastPathComponent ?? "No file chosen")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)

            if let createError {
                Text(createError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                if isCreating {
                    ProgressView().controlSize(.small)
                }
                Spacer()
                Button("Create") { create() }
                    .disabled(!isReady)
            }
        }
    }

    private var isReady: Bool {
        referenceURL != nil
            && !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isCreating
    }

    private func chooseReference() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if let nef = UTType(filenameExtension: "nef") {
            panel.allowedContentTypes = [nef]
        }
        if panel.runModal() == .OK {
            referenceURL = panel.url
        }
    }

    private func create() {
        guard let referenceURL else { return }
        isCreating = true
        createError = nil
        Task {
            let result = await flatField.create(reference: referenceURL, name: name)
            isCreating = false
            switch result {
            case .success:
                name = ""
                self.referenceURL = nil
            case .failure(_, let message):
                createError = message
            }
        }
    }

    private func delete(_ profile: FlatFieldProfile) async {
        deleteError = nil
        let result = await flatField.delete(profile)
        if case .failure(_, let message) = result {
            deleteError = message
        }
    }
}
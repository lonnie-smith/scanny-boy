import SwiftUI
import UniformTypeIdentifiers

/// The rig profile manager: the profile list, each row with a trash
/// button (confirming — a deleted profile cannot be recovered, and the
/// CLI's `RIG_PROFILE_IN_USE` refusal for profiles locked into a
/// roll's invariants arrives here as an alert), plus New Profile… — an
/// `NSOpenPanel` limited to NEF, a name field, and Create with a spinner,
/// since fitting a calibration runs for minutes. Modeled on `NewRollSheet`.
struct FlatFieldProfilesSheet: View {
    let rig: RigModel

    @Environment(\.dismiss) private var dismiss

    @State private var calibrationURLs: [URL] = []
    @State private var name = ""
    @State private var createError: String?
    @State private var deleteError: String?
    @State private var profilePendingDeletion: RigProfile?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Manage Scanning Rig Profiles").font(.title2.bold())

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
                    .disabled(rig.isCreating)
            }
        }
        .padding(20)
        .frame(minWidth: 460, minHeight: 320, maxHeight: 620)
        .interactiveDismissDisabled(rig.isCreating)
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
        if rig.profiles.isEmpty {
            Text("No profiles yet.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            VStack(spacing: 0) {
                ForEach(rig.profiles) { profile in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(profile.name)
                            Text(profile.calibrationSummary)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if let rejection = rejectionReason(of: profile) {
                                Text(rejection)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .help(rejection)
                            }
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

    /// A rejected fit's reason, surfaced so the automatic gates stay
    /// visible: the user has to be able to see that a correction was
    /// dropped and why.
    private func rejectionReason(of profile: RigProfile) -> String? {
        guard let distortion = profile.calibrationReport?.objectValue?["distortion"]?
            .objectValue, distortion["accepted"]?.boolValue == false
        else { return nil }
        return "Distortion: not applied — \(distortion["rejection_reason"]?.stringValue ?? "fit did not clear its gates")"
    }

    private var newProfile: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("New Profile").font(.headline)

            HStack {
                Button("Calibration Frames…") { chooseCalibrationFrames() }
                Text(calibrationSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if !calibrationURLs.isEmpty {
                    Button("Clear") { calibrationURLs = [] }
                        .buttonStyle(.borderless)
                }
            }

            Text(
                "16–20 shots of the ChArUco board, rotated 0/45/90/135° and translated so the pattern reaches every quadrant and every image corner."
            )
            .font(.caption2)
            .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 4) {
                Text("Profile Name")
                TextField("", text: $name)
                    .textFieldStyle(.roundedBorder)
            }

            if let createError {
                Text(createError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                if rig.isCreating {
                    if let progress = rig.creationProgress {
                        VStack(alignment: .leading, spacing: 2) {
                            ProgressView(
                                value: Double(progress.completed),
                                total: Double(max(progress.total, 1))
                            )
                            Text("Calibrating — \(phaseLabel(progress.phase))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        ProgressView().controlSize(.small)
                    }
                }
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
        !calibrationURLs.isEmpty
            || !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var calibrationSummary: String {
        calibrationURLs.isEmpty
            ? "No frames selected"
            : "\(calibrationURLs.count) frame\(calibrationURLs.count == 1 ? "" : "s") selected"
    }

    private func phaseLabel(_ phase: String) -> String {
        switch phase {
        case "detect": "detecting calibration frames"
        case "fit": "fitting distortion"
        case "chromatic": "measuring chromatic aberration"
        default: phase
        }
    }

    private var isReady: Bool {
        !calibrationURLs.isEmpty
            && !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !rig.isCreating
    }

    private func chooseCalibrationFrames() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        if let nef = UTType(filenameExtension: "nef") {
            panel.allowedContentTypes = [nef]
        }
        if panel.runModal() == .OK {
            calibrationURLs = panel.urls.sorted { $0.lastPathComponent < $1.lastPathComponent }
        }
    }

    private func create() {
        createError = nil
        let frames = calibrationURLs
        Task {
            let result = await rig.create(name: name, calibrationFrames: frames)
            switch result {
            case .success:
                name = ""
                calibrationURLs = []
            case .failure(_, let message):
                createError = message
            }
        }
    }

    private func delete(_ profile: RigProfile) async {
        deleteError = nil
        let result = await rig.delete(profile)
        if case .failure(_, let message) = result {
            deleteError = message
        }
    }
}

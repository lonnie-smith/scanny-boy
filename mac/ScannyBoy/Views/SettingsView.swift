import AppKit
import SwiftUI

/// Section 3.1: the library base is relocatable through a Settings window.
/// Relocating changes where the app looks; it never moves files.
struct SettingsView: View {
    let library: RollLibrary
    @Bindable var capture: CaptureSessionModel
    @Bindable var frameLog: FrameLogConnectionModel

    var body: some View {
        Form {
            Section("Library") {
                HStack {
                    Text(library.libraryBase.path)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Choose…") { chooseLibraryBase() }
                }
                Text("Every roll lives directly under this folder.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Capture") {
                HStack {
                    Text(capture.captureBaseFolder.path)
                        .font(.caption)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    Button("Choose…") { chooseCaptureBase() }
                }
                Text("Tethered NEFs land in a subfolder named after each roll.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Picker("Destination", selection: $capture.destination) {
                    Text("Camera buffer").tag(CaptureDestination.buffer)
                    Text("Memory card").tag(CaptureDestination.card)
                }
                Toggle("Audible cues", isOn: $capture.cuesEnabled)
            }
            Section("Frame log") {
                TextField("Base URL", text: $frameLog.baseURL)
                    .onSubmit { frameLog.commitBaseURL() }
                if let prefix = frameLog.tokenPrefix {
                    LabeledContent("Token") {
                        HStack {
                            Text("\(prefix)…")
                                .font(.system(.caption, design: .monospaced))
                            Button("Remove") { frameLog.removeToken() }
                        }
                    }
                } else {
                    SecureField("Token", text: $frameLog.tokenDraft)
                        .onSubmit { frameLog.saveToken() }
                    HStack {
                        Spacer()
                        Button("Save Token") { frameLog.saveToken() }
                            .disabled(frameLog.tokenDraft.isEmpty)
                    }
                }
                HStack {
                    Button("Test Connection") {
                        Task { await frameLog.testConnection() }
                    }
                    .disabled(frameLog.testState == .testing)
                    frameLogTestStatus
                }
                if let message = frameLog.errorMessage {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
                Text("The token is kept in the Keychain.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .padding()
        .frame(width: 480)
    }

    @ViewBuilder
    private var frameLogTestStatus: some View {
        switch frameLog.testState {
        case .idle:
            EmptyView()
        case .testing:
            ProgressView()
                .controlSize(.small)
        case .connected(let rolls):
            Text("Connected — \(rolls) rolls")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .failed(let message):
            Text(message)
                .font(.caption)
                .foregroundStyle(.red)
        }
    }

    private func chooseLibraryBase() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.directoryURL = library.libraryBase
        guard panel.runModal() == .OK, let url = panel.url else { return }
        library.libraryBase = url
    }

    private func chooseCaptureBase() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.directoryURL = capture.captureBaseFolder
        guard panel.runModal() == .OK, let url = panel.url else { return }
        capture.captureBaseFolder = url
    }
}

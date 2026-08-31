import AppKit
import SwiftUI

/// Section 3.1: the library base is relocatable through a Settings window.
/// Relocating changes where the app looks; it never moves files.
struct SettingsView: View {
    let library: RollLibrary

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
        }
        .formStyle(.grouped)
        .padding()
        .frame(width: 480)
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
}

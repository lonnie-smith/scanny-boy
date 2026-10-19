import SwiftUI

/// The app's version (`CFBundleShortVersionString`), shown at the trailing
/// edge of the window toolbar.
private struct VersionToolbarModifier: ViewModifier {
    private static let version =
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String

    func body(content: Content) -> some View {
        content.toolbar {
            if let version = Self.version {
                ToolbarItem(placement: .primaryAction) {
                    Text("v\(version)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("appVersionLabel")
                }
            }
        }
    }
}

extension View {
    /// Adds the app version label to the trailing end of the toolbar.
    func versionToolbarItem() -> some View {
        modifier(VersionToolbarModifier())
    }
}

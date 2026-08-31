import SwiftUI

/// Section 3.10: creating a roll asks for a name and a shots-per-negative,
/// and nothing else — no location, since every roll lives under the library
/// base (section 3.1).
struct NewRollSheet: View {
    let library: RollLibrary
    /// Called with the newly created roll, right before the sheet dismisses.
    let onCreated: (Roll) -> Void

    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var shotsPerNegative = 3
    @State private var isCreating = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New Roll").font(.title2.bold())

            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("newRollNameField")

            Stepper("Shots per negative: \(shotsPerNegative)", value: $shotsPerNegative, in: 1...12)

            if let errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            HStack {
                if isCreating {
                    ProgressView().controlSize(.small)
                }
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Create") { create() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(isReady == false)
                    .accessibilityIdentifier("createRollButton")
            }
        }
        .padding(20)
        .frame(minWidth: 360)
    }

    private var isReady: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isCreating
    }

    private func create() {
        isCreating = true
        errorMessage = nil
        Task {
            let result = await library.createRoll(name: name, shotsPerNegative: shotsPerNegative)
            isCreating = false
            switch result {
            case .success(let roll):
                onCreated(roll)
                dismiss()
            case .failure(_, let message):
                errorMessage = message
            }
        }
    }
}

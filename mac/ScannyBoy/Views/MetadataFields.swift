import SwiftUI

/// `YYYY-MM-DD` <-> `Date` conversion for the metadata date pickers. Noon
/// is the canonical time-of-day the roll's rank formula anchors on
/// (`roll_sequence.NOON`), so a picked date synthesizes the same instant
/// the CLI's formula would.
enum MetadataDate {
    static func date(fromISO iso: String?) -> Date? {
        guard let iso else { return nil }
        let components = iso.split(separator: "-").compactMap { Int($0) }
        guard components.count == 3 else { return nil }
        var dateComponents = DateComponents()
        dateComponents.year = components[0]
        dateComponents.month = components[1]
        dateComponents.day = components[2]
        dateComponents.hour = 12
        return Calendar.current.date(from: dateComponents)
    }

    static func isoString(from date: Date) -> String {
        let components = Calendar.current.dateComponents(
            [.year, .month, .day], from: date
        )
        return String(
            format: "%04d-%02d-%02d",
            components.year ?? 1970,
            components.month ?? 1,
            components.day ?? 1
        )
    }

    static let displayedComponents: DatePicker<EmptyView>.Components = [.date]
}

/// A text field that persists on blur: the value commits when focus leaves
/// (and on Return), never on every keystroke — each commit is one CLI
/// round trip, and the field must not fire one per character. An emptied
/// field commits `nil`, which the CLI reads as *clear* (a cleared
/// negative-level field then inherits the roll's fallback again).
///
/// The external truth is passed in as `committedValue` rather than bound:
/// the field is its own editor while focused, and external updates (a
/// refresh landing mid-edit, a selection change) re-sync the text only
/// while the field is not being edited.
struct CommitTextField: View {
    let title: String
    let committedValue: String?
    /// Shown in place of the value while empty — "<mixed values>" when the
    /// selection's values differ, the field's own hint otherwise.
    var prompt: String?
    let onCommit: (String?) -> Void

    @State private var text: String
    @FocusState private var focused: Bool

    init(
        title: String,
        committedValue: String?,
        prompt: String? = nil,
        onCommit: @escaping (String?) -> Void
    ) {
        self.title = title
        self.committedValue = committedValue
        self.prompt = prompt
        self.onCommit = onCommit
        _text = State(initialValue: committedValue ?? "")
    }

    var body: some View {
        TextField(title, text: $text, prompt: prompt.map { Text($0) })
            .focused($focused)
            .onSubmit { commit() }
            .onChange(of: focused) { _, isFocused in
                if isFocused {
                    // Editing starts from the committed value, not from
                    // whatever stale text a previous edit left behind.
                    text = committedValue ?? ""
                } else {
                    commit()
                }
            }
            .onChange(of: committedValue) { _, newValue in
                if !focused {
                    text = newValue ?? ""
                }
            }
    }

    private func commit() {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let newValue: String? = trimmed.isEmpty ? nil : trimmed
        if newValue != committedValue {
            onCommit(newValue)
        }
    }
}

/// A canonical-value field with typeahead: while the user types, the
/// catalog's previously-entered values (most-recently-used first) that
/// match the text are offered below the field; picking one commits it.
/// Typing a brand-new value is equally legitimate — committing adds it to
/// the catalog for next time. Caption deliberately does not use this: it
/// is prose, never cataloged.
struct TypeaheadField: View {
    let title: String
    let committedValue: String?
    let suggestions: [String]
    var prompt: String?
    let onCommit: (String?) -> Void

    @State private var text: String
    @FocusState private var focused: Bool
    @State private var pickedSuggestion: String?

    init(
        title: String,
        committedValue: String?,
        suggestions: [String],
        prompt: String? = nil,
        onCommit: @escaping (String?) -> Void
    ) {
        self.title = title
        self.committedValue = committedValue
        self.suggestions = suggestions
        self.prompt = prompt
        self.onCommit = onCommit
        _text = State(initialValue: committedValue ?? "")
    }

    private var filteredSuggestions: [String] {
        let query = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return [] }
        return Array(
            suggestions
                .filter { $0.localizedCaseInsensitiveContains(query) }
                // The typed text itself is not a suggestion, it is a commit.
                .filter { $0.caseInsensitiveCompare(query) != .orderedSame }
                .prefix(6)
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField(title, text: $text, prompt: prompt.map { Text($0) })
                .focused($focused)
                .onSubmit { commit() }
                .onChange(of: focused) { _, isFocused in
                    if isFocused {
                        text = committedValue ?? ""
                        pickedSuggestion = nil
                    } else {
                        commit()
                    }
                }
                .onChange(of: committedValue) { _, newValue in
                    if !focused {
                        text = newValue ?? ""
                    }
                }
            if focused, !filteredSuggestions.isEmpty {
                // A plain list under the field: small enough to stay a
                // popover-free overlay, keyboard-free by design — the
                // arrow keys stay owned by the filmstrip navigation.
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(filteredSuggestions, id: \.self) { suggestion in
                        Button {
                            pickedSuggestion = suggestion
                            text = suggestion
                            commit()
                        } label: {
                            Text(suggestion)
                                .lineLimit(1)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .padding(.vertical, 3)
                        .padding(.horizontal, 6)
                    }
                }
                .background(.background)
                .clipShape(RoundedRectangle(cornerRadius: 5))
                .overlay {
                    RoundedRectangle(cornerRadius: 5)
                        .strokeBorder(.quaternary)
                }
                .zIndex(1)
            }
        }
    }

    private func commit() {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let newValue: String? = trimmed.isEmpty ? nil : trimmed
        if newValue != committedValue || pickedSuggestion != nil {
            onCommit(newValue)
        }
        pickedSuggestion = nil
    }
}

/// A date field that persists on change (a picker commits by its nature —
/// a click on a calendar day *is* the committed gesture, there is no blur
/// halfway through one). Internal `@State` is re-synced from the external
/// value only when it actually differs, so the `onChange` that fires from
/// that re-sync cannot loop back into another commit.
struct CommitDatePicker: View {
    let title: String
    /// The date the field displays, as `YYYY-MM-DD`; `nil` shows a
    /// disabled picker plus a hint (there is no DatePicker placeholder).
    let committedValue: String?
    let onCommit: (String?) -> Void

    @State private var date: Date

    init(title: String, committedValue: String?, onCommit: @escaping (String?) -> Void) {
        self.title = title
        self.committedValue = committedValue
        self.onCommit = onCommit
        _date = State(
            initialValue: MetadataDate.date(fromISO: committedValue)
                ?? Date.now
        )
    }

    var body: some View {
        HStack {
            DatePicker(
                title,
                selection: $date,
                displayedComponents: MetadataDate.displayedComponents
            )
            .onChange(of: date) { _, newDate in
                let iso = MetadataDate.isoString(from: newDate)
                if iso != committedValue {
                    onCommit(iso)
                }
            }
            if committedValue != nil {
                Button("Clear") { onCommit(nil) }
                    .help("Clear this date")
            }
        }
    }
}

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

/// What a metadata field hands its commit to: the write, returned as the
/// task that finishes when it has landed or failed, so the field can keep
/// showing the committed text until then and fall back to the stored
/// value if the write did not take. `nil` means nothing was written.
typealias MetadataCommit = (String?) -> Task<Void, Never>?

/// A text field that persists on blur: the value commits when focus leaves
/// (and on Return), never on every keystroke — each commit is one CLI
/// round trip, and the field must not fire one per character. An emptied
/// field commits `nil`, which the CLI reads as *clear* (a cleared
/// negative-level field then inherits the roll's fallback again).
///
/// The external truth is passed in as `committedValue` rather than bound:
/// the field is its own editor while focused, and external updates (a
/// refresh landing mid-edit) re-sync the text only while the field is not
/// being edited and has no write of its own outstanding.
///
/// An edit belongs to what was on screen when it began: the commit action
/// and starting value are captured when focus arrives. Clicking another
/// frame in the filmstrip does not take focus from the field, so when
/// `editTarget` changes mid-edit the edit so far is committed to the old
/// target, and editing carries on against the new one.
///
/// With `suggestions`, the field offers typeahead: while the user types,
/// the catalog's previously-entered values (most-recently-used first) that
/// match the text are listed below it; picking one commits it. Typing a
/// brand-new value is equally legitimate — committing adds it to the
/// catalog for next time. Caption deliberately has none: it is prose,
/// never cataloged.
struct CommitTextField: View {
    let title: String
    let committedValue: String?
    let suggestions: [String]
    /// Shown in place of the value while empty — "<mixed values>" when the
    /// selection's values differ, the field's own hint otherwise.
    let prompt: String?
    /// What the field edits — the roll, or the selected negatives' IDs.
    let editTarget: AnyHashable?
    let onCommit: MetadataCommit

    @State private var text: String
    @FocusState private var focused: Bool
    @State private var pickedSuggestion: String?
    @State private var edit: Edit?
    /// The last committed text whose write has not finished yet.
    @State private var inFlight: InFlight?

    /// The edit in progress: where it started and where it commits to.
    private struct Edit {
        var baseline: String?
        let target: AnyHashable?
        let commit: MetadataCommit
    }

    private struct InFlight: Equatable {
        let id = UUID()
        let value: String?
    }

    init(
        title: String,
        committedValue: String?,
        suggestions: [String] = [],
        prompt: String? = nil,
        editTarget: AnyHashable? = nil,
        onCommit: @escaping MetadataCommit
    ) {
        self.title = title
        self.committedValue = committedValue
        self.suggestions = suggestions
        self.prompt = prompt
        self.editTarget = editTarget
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
                        beginEdit()
                    } else {
                        commit()
                        edit = nil
                        pickedSuggestion = nil
                    }
                }
                .onChange(of: committedValue) { _, newValue in
                    if !focused, inFlight == nil {
                        text = newValue ?? ""
                    }
                }
                .onChange(of: editTarget) { _, _ in
                    guard focused else { return }
                    commit()
                    beginEdit()
                }
                .onChange(of: inFlight) { _, newValue in
                    // The write finished: show what was actually stored.
                    if newValue == nil, !focused {
                        text = committedValue ?? ""
                    }
                }
            if focused, !filteredSuggestions.isEmpty {
                // A plain list under the field, keyboard-free by design —
                // the arrow keys stay owned by the text field while it is
                // focused, and the rows never take focus from it.
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
                        .focusable(false)
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
            }
        }
    }

    /// Editing starts from the committed value (or this field's own write
    /// still in flight), not from whatever stale text an earlier edit left
    /// behind.
    private func beginEdit() {
        let start = inFlight.map(\.value) ?? committedValue
        text = start ?? ""
        pickedSuggestion = nil
        edit = Edit(baseline: start, target: editTarget, commit: onCommit)
    }

    private func commit() {
        let current = edit ?? Edit(baseline: committedValue, target: editTarget, commit: onCommit)
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let newValue: String? = trimmed.isEmpty ? nil : trimmed
        let picked = pickedSuggestion != nil
        pickedSuggestion = nil
        guard newValue != current.baseline || picked else { return }
        // Return then blur must not write the same value twice.
        edit?.baseline = newValue
        guard let task = current.commit(newValue) else { return }
        let pending = InFlight(value: newValue)
        inFlight = pending
        Task {
            await task.value
            if inFlight == pending { inFlight = nil }
        }
    }
}

/// A date field that commits once per deliberate choice. The date shows as
/// text; the calendar button opens an editor (a typeable field and a
/// calendar, both on one working date) that writes only on Set — never per
/// keystroke or per stepper click, each of which would otherwise be its
/// own CLI write of a half-typed date. Set also commits a date that was
/// already showing in the editor, so "today" or the roll's date can be
/// chosen as-is. Dismissing the editor any other way changes nothing.
struct CommitDatePicker: View {
    let title: String
    /// The date the field displays, as `YYYY-MM-DD`; `nil` shows a dash
    /// rather than today's date.
    let committedValue: String?
    /// The date the editor opens on when there is no committed one — the
    /// roll's date for an image that follows it. Today when `nil`.
    let suggestedValue: String?
    /// When false, the parent supplies the label (stacked layout).
    let showsTitle: Bool
    let onCommit: (String?) -> Void

    @State private var workingDate = Date.now
    @State private var isChoosingDate = false

    init(
        title: String,
        committedValue: String?,
        suggestedValue: String? = nil,
        showsTitle: Bool = true,
        onCommit: @escaping (String?) -> Void
    ) {
        self.title = title
        self.committedValue = committedValue
        self.suggestedValue = suggestedValue
        self.showsTitle = showsTitle
        self.onCommit = onCommit
    }

    var body: some View {
        HStack {
            if showsTitle {
                Text(title)
            }
            if let date = MetadataDate.date(fromISO: committedValue) {
                Text(date, format: .dateTime.year().month().day())
            } else {
                Text("—")
                    .foregroundStyle(.tertiary)
            }
            Button("", systemImage: "calendar") {
                workingDate = MetadataDate.date(fromISO: committedValue)
                    ?? MetadataDate.date(fromISO: suggestedValue)
                    ?? Date.now
                isChoosingDate = true
            }
            .buttonStyle(.borderless)
            .help(committedValue == nil ? "Set date" : "Change date")
            .popover(isPresented: $isChoosingDate) {
                editor
            }
            if committedValue != nil {
                Button("Clear") { onCommit(nil) }
                    .help("Clear this date")
            }
        }
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: 10) {
            DatePicker(
                title,
                selection: $workingDate,
                displayedComponents: MetadataDate.displayedComponents
            )
            .datePickerStyle(.field)
            .labelsHidden()
            DatePicker(
                title,
                selection: $workingDate,
                displayedComponents: MetadataDate.displayedComponents
            )
            .datePickerStyle(.graphical)
            .labelsHidden()
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) {
                    isChoosingDate = false
                }
                .keyboardShortcut(.cancelAction)
                Button("Set") {
                    let iso = MetadataDate.isoString(from: workingDate)
                    isChoosingDate = false
                    if iso != committedValue {
                        onCommit(iso)
                    }
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding()
    }
}

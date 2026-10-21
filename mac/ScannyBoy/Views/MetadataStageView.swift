import AppKit
import SwiftUI

/// The Metadata tab: the roll's editable metadata at the top, then the
/// same browser the Edit tab uses — filmstrip at the bottom, the selected
/// negative large, and the per-image metadata fields beside the preview.
///
/// Every field persists on blur (or on change, for a picker), straight to
/// the library database through `metadata set` — nothing here touches a
/// TIFF. Metadata reaches TIFFs only at export. The roll-level values are
/// live fallbacks: an image without its own explicit value shows and
/// exports the roll's value, so setting a roll field covers every image
/// that has not been given its own — and never overwrites one that has.
///
/// The old dirty-count/Apply section is gone: the intended capture time is
/// pure database state now, and the exporter writes it into the exported
/// TIFF directly.
struct MetadataStageView: View {
    let library: RollLibrary
    @Bindable var edit: EditModel
    let run: RunModel
    let activity: AppActivity

    @State private var renameError: String?

    var body: some View {
        VStack(spacing: 0) {
            rollSection
            Divider()
            browserSection
            Divider()
            FilmstripView(
                negatives: edit.visibleNegatives,
                cameraColor: edit.roll?.cameraColor,
                highlightLock: edit.roll?.highlightLock,
                isSelected: edit.isSelected,
                warningIDs: []
            ) { negativeID, additive, extendingRange in
                // A thumbnail click does not take focus from a text field,
                // so end the edit first: it commits to the frames it began
                // on, and ⌘A then selects images rather than field text.
                AppKeyboard.endTextEditing()
                edit.select(
                    negativeID,
                    additive: additive,
                    extendingRange: extendingRange
                )
            }
        }
        // Clicking empty space ends text editing, like clicking the
        // thumbnails or the preview does. The fields sit in front of this
        // and keep their own clicks.
        .background {
            Color.clear
                .contentShape(Rectangle())
                .onTapGesture { AppKeyboard.endTextEditing() }
        }
        .disabled(activity.isBusy)
        // AppKit can hand the first text field (the roll name) focus as the
        // tab appears, and a focused field claims ⌘A for its own text. Let
        // go of it once the tab has settled, so ⌘A selects every image
        // until the user clicks a field themselves.
        .onAppear {
            DispatchQueue.main.async {
                MainActor.assumeIsolated { AppKeyboard.endTextEditing() }
            }
        }
        // `initial: true` matters: a run usually finishes while this tab is
        // not mounted, so the phase can already be `.finished` when the tab
        // first appears — the roll state it would otherwise show is stale.
        .onChange(of: run.phase, initial: true) { _, phase in
            if phase == .finished { edit.refresh() }
        }
        .task {
            edit.refreshCatalog()
        }
    }

    // MARK: - Roll section

    /// Every roll field is visible at once, in a three-column grid: the
    /// content is shown whole when it fits, and scrolls only in a window
    /// too short for it.
    private var rollSection: some View {
        ViewThatFits(in: .vertical) {
            rollSectionContent
            ScrollView { rollSectionContent }
        }
    }

    private var rollSectionContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let roll = edit.roll {
                rollFields(roll)
            } else {
                ContentUnavailableView(
                    "Roll Not Loaded",
                    systemImage: "photo.stack",
                    description: Text("Select a roll to edit its metadata.")
                )
            }
            if let renameError {
                IssueLabel(
                    issue: ConfigurationModel.Issue(
                        code: .rollRenameFailed, message: renameError
                    ),
                    style: .error
                )
            }
            if let metadataError = edit.metadataError {
                IssueLabel(issue: metadataError, style: .error)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
    }

    private static let rollFieldWidth: CGFloat = 200
    private static let rollGridSpacing: CGFloat = 24

    @ViewBuilder
    private func rollFields(_ roll: RollManifest) -> some View {
        Text("Roll metadata")
            .font(.headline)

        Grid(
            alignment: .topLeading,
            horizontalSpacing: Self.rollGridSpacing,
            verticalSpacing: 10
        ) {
            GridRow {
                rollField("Name") {
                    CommitTextField(
                        title: "Name",
                        committedValue: roll.rollName,
                        prompt: "Roll name",
                        editTarget: AnyHashable(roll.rollID)
                    ) { newName in
                        commitRename(roll, to: newName)
                    }
                    .frame(width: Self.rollFieldWidth)
                }
                if let rollURL = edit.rollURL {
                    rollField("Folder") {
                        HStack {
                            Text(rollURL.path)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Button("Open") {
                                NSWorkspace.shared.activateFileViewerSelecting([rollURL])
                            }
                        }
                        .frame(maxWidth: Self.rollFieldWidth, alignment: .leading)
                    }
                } else {
                    Color.clear
                        .gridCellUnsizedAxes([.horizontal, .vertical])
                }
                rollField("Capture date") {
                    CommitDatePicker(
                        title: "Capture date",
                        committedValue: roll.metadata.rollCaptureDate
                    ) { date in
                        edit.setRollCaptureDate(date)
                    }
                }
            }
            GridRow {
                rollTextField(roll, field: "film")
                rollTextField(roll, field: "iso")
                rollTextField(roll, field: "camera")
            }
            GridRow {
                rollTextField(roll, field: "lens")
                rollTextField(roll, field: "city")
                rollTextField(roll, field: "state")
            }
            GridRow {
                // Prose, never cataloged: no suggestions (the catalog holds
                // no caption values).
                rollTextField(roll, field: "caption", width: Self.captionWidth)
                    .gridCellColumns(3)
            }
        }

        Text(
            "Roll-level values apply to every image without its own value. "
                + "Lens, city, state and caption can be overridden per image; "
                + "film, ISO and camera apply to the whole roll."
        )
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    /// The caption field spans the grid's three columns.
    private static let captionWidth: CGFloat =
        3 * rollFieldWidth + 2 * rollGridSpacing

    /// One roll-level text field, committing straight to the roll's value.
    private func rollTextField(
        _ roll: RollManifest, field: String, width: CGFloat = MetadataStageView.rollFieldWidth
    ) -> some View {
        rollField(fieldLabel(field)) {
            CommitTextField(
                title: fieldLabel(field),
                committedValue: roll.metadata[field],
                suggestions: edit.catalog[field] ?? [],
                editTarget: AnyHashable(roll.rollID)
            ) { value in
                edit.setRollField(field, to: value)
            }
            .frame(width: width)
        }
    }

    private func rollField(_ label: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            content()
        }
    }

    /// An emptied name is not a rename: nothing is written, and the field
    /// falls back to the roll's current name.
    private func commitRename(_ roll: RollManifest, to newName: String?) -> Task<Void, Never>? {
        guard let newName else { return nil }
        guard let libraryRoll = library.rolls.first(where: { $0.rollID == roll.rollID }) else {
            renameError = "The roll could not be found in the library."
            return nil
        }
        return Task {
            do {
                let renamed = try await library.renameRoll(
                    libraryRoll, to: newName, runIsActive: activity.isBusy
                )
                renameError = nil
                // A name that slugs to the same folder leaves the roll's
                // URL unchanged, so the rescan re-reads nothing; without
                // this the field would keep showing the old name. A moved
                // folder is re-read when the rescan hands over its new URL.
                if renamed.path.standardizedFileURL.path
                    == edit.rollURL?.standardizedFileURL.path
                {
                    edit.refresh()
                }
            } catch {
                renameError = error.localizedDescription
            }
        }
    }

    // MARK: - Per-image browser

    @ViewBuilder
    private var browserSection: some View {
        if let negative = edit.selectedNegative {
            HStack(spacing: 0) {
                PreviewImageView(
                    negative: negative,
                    cameraColor: edit.roll?.cameraColor,
                    highlightLock: edit.roll?.highlightLock
                )
                    .padding()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(Rectangle())
                    .onTapGesture { AppKeyboard.endTextEditing() }
                Divider()
                imageMetadataForm
                    .frame(width: 340)
            }
        } else {
            ContentUnavailableView(
                "No Negatives Yet",
                systemImage: "photo.stack",
                description: Text("Convert scans into this roll to set their metadata.")
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var filenameHeader: String {
        edit.selectedNegative?.expectedOutput ?? ""
    }

    private var selectionCaption: String? {
        let count = edit.selectionTargets.count
        return count > 1 ? "\(count) selected" : nil
    }

    /// The per-image metadata fields, acting on the whole selection when
    /// one exists (falling back to the anchor alone, exactly like the Edit
    /// tab's controls). Fields whose values differ across the selection
    /// show "<mixed values>" until something new is committed — which then
    /// applies to every selected image in one batch.
    private var imageMetadataForm: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(filenameHeader)
                        .font(.headline)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let selectionCaption {
                        Text(selectionCaption)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                negativeCaptureDateField
                Divider()
                ForEach(RollManifest.metadataFields, id: \.self) { field in
                    negativeMetadataField(field)
                }
                if edit.isSettingMetadata {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Saving…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding()
        }
    }

    private var targets: [RollManifest.Negative] { edit.selectionTargets }

    /// The images' capture-date override. `nil` means "follow the roll's
    /// date" — the caption says which date the image(s) effectively carry.
    private var negativeCaptureDateField: some View {
        VStack(alignment: .leading, spacing: 4) {
            rollField("Capture date") {
                CommitDatePicker(
                    title: "Capture date",
                    committedValue: singleOverride,
                    suggestedValue: edit.roll?.metadata.rollCaptureDate,
                    showsTitle: false
                ) { date in
                    edit.setNegativeCaptureDate(targets, to: date)
                }
                .disabled(targets.isEmpty)
            }
            if let caption = captureDateCaption {
                Text(caption)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// The selection's date override when all selected images share one
    /// (including "none"); `nil` when mixed or unset.
    private var singleOverride: String? {
        let overrides = targets.map { $0.captureTime.dateOverride }
        let distinct = Set(overrides)
        return distinct.count == 1 ? overrides.first ?? nil : nil
    }

    private var captureDateCaption: String? {
        let rollDate = edit.roll?.metadata.rollCaptureDate
        if targets.isEmpty {
            return "No image selected."
        }
        let overrides = Set(targets.map { $0.captureTime.dateOverride })
        if overrides.contains(nil), overrides.count == 1, let rollDate {
            return "Following the roll date (\(rollDate))."
        }
        if overrides.contains(nil), overrides.count == 1 {
            return nil
        }
        return overrides.count == 1
            ? "Override set for \(targets.count) image(s)."
            : "Mixed dates. Setting a date applies to all selected images."
    }

    private func negativeMetadataField(_ field: String) -> some View {
        // Captured now: `targets` reads the live selection, and the field
        // commits an edit to the frames it was begun on.
        let targets = self.targets
        let values = edit.effectiveValues(of: targets, field: field)
        let isMixed = values.count > 1
        let committed: String? = isMixed ? nil : values.first ?? nil
        // No image has its own value, so every one shows the roll's.
        let followsRoll = committed != nil && targets.allSatisfy { $0.metadata[field] == nil }
        return VStack(alignment: .leading, spacing: 2) {
            rollField(fieldLabel(field)) {
                CommitTextField(
                    title: fieldLabel(field),
                    committedValue: committed,
                    suggestions: edit.catalog[field] ?? [],
                    prompt: isMixed ? "<mixed values>" : nil,
                    editTarget: AnyHashable(targets.map(\.negativeID))
                ) { value in
                    edit.setNegativeField(targets, field: field, to: value)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if followsRoll {
                Text("From roll")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .disabled(targets.isEmpty)
    }

    private func fieldLabel(_ field: String) -> String {
        if field == "iso" { return "ISO" }
        return field.prefix(1).uppercased() + field.dropFirst()
    }
}

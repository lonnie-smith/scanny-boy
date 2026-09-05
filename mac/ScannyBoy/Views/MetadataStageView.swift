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
                isSelected: edit.isSelected
            ) { negativeID, additive, extendingRange in
                edit.select(
                    negativeID,
                    additive: additive,
                    extendingRange: extendingRange
                )
            }
        }
        .background {
            SelectionShortcutButtons(
                onPrevious: edit.selectPrevious,
                onNext: edit.selectNext,
                onSelectAll: edit.selectAll,
                onDeselectAll: edit.deselectAll
            )
        }
        .disabled(activity.isBusy)
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

    private var rollSection: some View {
        ScrollView {
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
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
        }
        .frame(maxHeight: 240)
    }

    @ViewBuilder
    private func rollFields(_ roll: RollManifest) -> some View {
        Text("Roll metadata")
            .font(.headline)

        HStack(alignment: .top, spacing: 32) {
            VStack(alignment: .leading, spacing: 10) {
                rollField("Name") {
                    CommitTextField(
                        title: "Name",
                        committedValue: roll.rollName,
                        prompt: "Roll name"
                    ) { newName in
                        commitRename(roll, to: newName)
                    }
                    .frame(maxWidth: 280)
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
                    }
                }
                rollField("Capture date") {
                    CommitDatePicker(
                        title: "Capture date",
                        committedValue: roll.metadata.rollCaptureDate
                    ) { date in
                        Task { await edit.setRollCaptureDate(date) }
                    }
                }
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(EditModel.catalogedFields, id: \.self) { field in
                    rollField(fieldLabel(field)) {
                        TypeaheadField(
                            title: fieldLabel(field),
                            committedValue: roll.metadata[field],
                            suggestions: edit.catalog[field] ?? []
                        ) { value in
                            Task { await edit.setRollField(field, to: value) }
                        }
                        .frame(maxWidth: 280)
                    }
                }
            }
        }

        Text(
            "Roll-level values apply to every image without its own value. "
                + "Images keep any value set individually."
        )
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private func rollField(_ label: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            content()
        }
    }

    private func commitRename(_ roll: RollManifest, to newName: String?) {
        guard let newName else { return }
        guard let libraryRoll = library.rolls.first(where: { $0.rollID == roll.rollID }) else {
            renameError = "The roll could not be found in the library."
            return
        }
        Task {
            do {
                _ = try await library.renameRoll(
                    libraryRoll, to: newName, runIsActive: activity.isBusy
                )
                renameError = nil
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
                PreviewImageView(negative: negative)
                    .padding()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
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
            CommitDatePicker(
                title: "Capture date",
                committedValue: singleOverride
            ) { date in
                Task { await edit.setNegativeCaptureDate(targets, to: date) }
            }
            .disabled(targets.isEmpty)
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
        let values = edit.effectiveValues(of: targets, field: field)
        let isMixed = values.count > 1
        let committed: String? = isMixed ? nil : values.first ?? nil
        return LabeledContent(fieldLabel(field)) {
            TypeaheadField(
                title: fieldLabel(field),
                committedValue: committed,
                suggestions: edit.catalog[field] ?? [],
                prompt: isMixed ? "<mixed values>" : nil
            ) { value in
                Task { await edit.setNegativeField(targets, field: field, to: value) }
            }
            .frame(maxWidth: 200)
        }
        .disabled(targets.isEmpty)
    }

    private func fieldLabel(_ field: String) -> String {
        field.prefix(1).uppercased() + field.dropFirst()
    }
}

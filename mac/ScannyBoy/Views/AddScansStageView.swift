import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// The Add Scans stage — folder selection, one-range selection, and the
/// grouping preview, plus Convert — shown in the Add Scans sheet for the roll
/// it was opened on. Every conversion from this stage writes into `roll`:
/// there is no output-folder picker, no film date, and no way to retarget
/// another roll, and the overwrite-confirmation dialog is replaced by the
/// overlap sheet.
///
/// Scans-per-negative is not a roll property: it is each stitch batch's own
/// choice, selected here and required before the Convert button enables — so
/// one roll can hold negatives stitched from different scan counts.
///
/// Convert adds live progress, cooperative Cancel, the completed/failed
/// negatives, Reveal in Finder, and the manifest the run left behind.
struct AddScansStageView: View {
    let library: RollLibrary
    let rig: RigModel
    let grid: GridModel
    @Bindable var model: ConfigurationModel
    let edit: EditModel
    let run: RunModel
    let activity: AppActivity
    /// The roll the sheet was opened on — every conversion from this stage writes into it.
    let roll: Roll

    @State private var isSelectingBaseFrameRegion = false
    @State private var isConfirmingConvert = false
    @State private var isConvertDropTarget = false

    /// `roll`, re-read from the library: it rescans after a run, so the
    /// negative count here is the fresh one.
    private var liveRoll: Roll {
        library.rolls.first { $0.id == roll.id } ?? roll
    }

    var body: some View {
        HSplitView {
            catalogueColumn
                .frame(minWidth: 280, idealWidth: 340, maxHeight: .infinity)
            detailColumn
                .frame(minWidth: 380, idealWidth: 460, maxHeight: .infinity)
        }
        // Without a full-height frame the split view sizes to the left
        // column's content — a `List` when the catalogue is populated, but
        // only the compact empty state otherwise — and the detail pane then
        // vertically centers that short stack instead of pinning it to the top.
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .disabled(activity.isBusy)
        .confirmationDialog(
            "Add scans to “\(liveRoll.displayName)”?",
            isPresented: $isConfirmingConvert
        ) {
            Button("Add to “\(liveRoll.displayName)”") { startRun() }
            Button("Cancel", role: .cancel) {}
        } message: {
            if let count = liveRoll.negativeCount {
                Text(
                    "This roll already contains \(Pluralize.count(count, "negative")). "
                        + "The new scans will be added to it."
                )
            }
        }
    }

    private var catalogueColumn: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Input folder").font(.headline)
                Spacer()
                Button("Choose…") { chooseInputFolder() }
            }
            if let inputFolder = model.inputFolder {
                Text(inputFolder.path)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            if model.catalogue.isEmpty {
                ContentUnavailableView(
                    "No NEF Files",
                    systemImage: "photo.on.rectangle.angled",
                    description: Text("Choose a folder of .NEF files to begin.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(model.catalogue, id: \.self, selection: $model.selectedFiles) { name in
                    CatalogueRow(
                        name: name,
                        url: model.fileURL(for: name),
                        selectedFiles: model.selectedFiles
                    )
                }
                .listStyle(.inset)
            }

            ForEach(model.catalogueWarnings, id: \.self) { warning in
                IssueLabel(issue: warning, style: .warning)
            }
            if let error = model.catalogueError {
                IssueLabel(issue: error, style: .error)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding()
        // Nothing about the configuration may change while a conversion is
        // using it.
        .disabled(activity.isBusy)
    }

    private var detailColumn: some View {
        Form {
            // While a probe is in flight the Stitch button's enablement is
            // not yet trustworthy — but the sections themselves stay
            // mounted (M3): a drag-select across the catalogue used to tear
            // the whole form down and rebuild it once per row, and the
            // grouping picker was unreachable for the duration.
            configurationSections
                .disabled(activity.isBusy)
            convertSection
        }
        .formStyle(.grouped)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding()
    }

    @ViewBuilder
    private var configurationSections: some View {
        Section {
            // Chosen fresh for every run: a roll does not lock to one
            // profile, so different runs into the same roll may each pick
            // a different one. Defaults to the last profile used, across
            // any roll.
            Picker("Scanning Rig Profile", selection: $model.rigProfileID) {
                Text("None").tag(String?.none)
                ForEach(rig.profiles) { profile in
                    Text(profile.name).tag(String?.some(profile.profileID))
                }
            }
            if model.rigProfileID == nil {
                Text("Optional: choose the geometric calibration measured for this copy stand.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            // `RollIntakeSheet` owns the profile sheets and answers this
            // notification (refreshing the rig first), as for Capture's.
            Button("Manage…") {
                NotificationCenter.default.post(name: .scannyBoyRequestFlatFieldProfiles, object: nil)
            }
            FlatFieldReferenceField(
                flatField: model.flatField,
                isBusy: activity.isBusy,
                isAnalyzing: model.isAttachingFlatFieldReference,
                error: model.flatFieldReferenceError,
                onChoose: { chooseFlatFieldReference(replace: false) },
                onReplace: { chooseFlatFieldReference(replace: true) },
                onDropFrame: { url in
                    Task { await model.attachFlatFieldReference(at: url) }
                },
                fileURL: model.fileURL(for:)
            )
            FilmKindField(
                filmKind: model.filmKind,
                isLocked: model.filmKindLocked,
                isBusy: activity.isBusy,
                error: model.filmKindError,
                onChoose: { choice in
                    Task { await model.setFilmKind(choice.rawValue) }
                }
            )
            if model.filmBase == nil, !model.requiresBaseFrame {
                LabeledContent("Film Base reference") {
                    Text(CaptureStageView.baseFrameNotNeeded)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else {
                baseFrameField
            }
            // The roll's Format and Auto-crop: the same stored values the
            // Capture sheet edits (`RollFormatFields`).
            RollFormatFields(model: model)
            Picker("Multi-shot scan configuration", selection: $model.gridProfileID) {
                Text("Choose…").tag(String?.none)
                ForEach(grid.profiles) { profile in
                    Text(profile.name).tag(String?.some(profile.profileID))
                }
            }
            if let profileID = model.gridProfileID,
                let profile = grid.profiles.first(where: { $0.profileID == profileID })
            {
                Text("\(profile.dimensionSummary) — \(profile.scanCount) scans per negative")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                Text("How the scans are arranged in each negative. Choose a configuration to enable Convert.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("perNegativeHint")
            }
            Button("Manage…") {
                NotificationCenter.default.post(name: .scannyBoyRequestGridProfiles, object: nil)
            }
            if !model.groups.isEmpty {
                GroupingPreview(groups: model.groups)
            }
            ForEach(model.selectionWarnings, id: \.self) { warning in
                IssueLabel(issue: warning, style: .warning)
            }
            if let error = model.selectionError {
                IssueLabel(issue: error, style: .error)
            }
            if let error = model.rollError {
                IssueLabel(issue: error, style: .error)
            }
        } header: {
            HStack {
                Text("Roll Setup")
                if model.isValidating || model.isAttachingBaseFrame || model.isAttachingFlatFieldReference {
                    Spacer()
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
    }

    private var baseFrameField: some View {
        BaseFrameField(
            filmBase: model.filmBase,
            isBusy: activity.isBusy,
            isAnalyzing: model.isAttachingBaseFrame,
            error: model.baseFrameError,
            onChoose: { chooseBaseFrame(replace: false) },
            onReplace: { chooseBaseFrame(replace: true) },
            onDropFrame: { url in
                Task { await model.attachBaseFrame(at: url) }
            },
            fileURL: model.fileURL(for:),
            onSelectRegion: selectBaseFrameRegion
        )
        .sheet(isPresented: $isSelectingBaseFrameRegion) {
            if let url = model.lastBaseFrameURL {
                BaseFrameRegionSheet(frameURL: url) { region in
                    Task { await model.attachBaseFrame(at: url, region: region) }
                }
            }
        }
    }

    private var selectBaseFrameRegion: (() -> Void)? {
        guard model.lastBaseFrameURL != nil else { return nil }
        return { isSelectingBaseFrameRegion = true }
    }

    private var convertSection: some View {
        Section("Convert") {
            convertDropZone
        }
    }

    private var convertDropZone: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Spacer()
                if run.isActive {
                    Button("Cancel", role: .destructive) { run.cancel() }
                        .disabled(!run.canCancel)
                }
                Button("Convert") { handleConvertTap() }
                    .disabled(!model.runEnabled || activity.isBusy)
                    .keyboardShortcut(.defaultAction)
            }
            // Add Scans shows results for its own invocations only (M9):
            // an apply-metadata started from the Metadata tab is not a
            // conversion, even though it shares the same `RunModel`.
            if run.phase != .idle, run.invocation != .applyMetadata {
                if run.isActive {
                    RunProgressView(run: run)
                } else if run.phase == .finishing {
                    FinishingView()
                } else {
                    RunResultView(run: run)
                }
            } else if !model.selectedFiles.isEmpty {
                Text(Self.convertReadyLabel(scanCount: model.selectedFiles.count))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            RoundedRectangle(cornerRadius: 8)
                .fill(isConvertDropTarget ? Color.accentColor.opacity(0.12) : Color.clear)
        }
        .overlay {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(
                    isConvertDropTarget ? Color.accentColor : Color.secondary.opacity(0.35),
                    style: StrokeStyle(
                        lineWidth: isConvertDropTarget ? 2 : 1,
                        dash: isConvertDropTarget ? [] : [5, 3]
                    )
                )
        }
        .contentShape(RoundedRectangle(cornerRadius: 8))
        .onDrop(of: [.plainText], isTargeted: $isConvertDropTarget) { providers in
            handleConvertDrop(providers)
        }
    }

    private func handleConvertDrop(_ providers: [NSItemProvider]) -> Bool {
        guard !activity.isBusy, let provider = providers.first else { return false }
        _ = provider.loadObject(ofClass: String.self) { string, _ in
            guard let string,
                let names = CatalogueDragSupport.decodeDragPayload(string)
            else { return }
            Task { @MainActor in
                applyConvertDrop(filenames: names)
            }
        }
        return true
    }

    private func handleConvertTap() {
        Task {
            guard await model.validateSelection() else { return }
            if Self.shouldConfirmConvert(into: liveRoll) {
                isConfirmingConvert = true
            } else {
                startRun()
            }
        }
    }

    @discardableResult
    private func applyConvertDrop(filenames: [String]) -> Bool {
        guard !activity.isBusy else { return false }
        let valid = Set(filenames.filter { model.catalogue.contains($0) })
        guard !valid.isEmpty else { return false }
        model.selectedFiles = valid
        return true
    }

    @discardableResult
    private func applyConvertDrop(urls: [URL]) -> Bool {
        guard !activity.isBusy, let inputFolder = model.inputFolder else { return false }
        let names = urls.compactMap { Self.resolveFileName($0, relativeTo: inputFolder) }
        return applyConvertDrop(filenames: names)
    }

    /// Whether Convert should ask before writing into an already-populated
    /// roll. Testable so the dialog gate stays decoupled from SwiftUI state.
    nonisolated static func shouldConfirmConvert(into roll: Roll?) -> Bool {
        guard let roll, roll.status == .ok else { return false }
        return (roll.negativeCount ?? 0) > 0
    }

    // A run always adopts whatever it overlaps in place — passing no
    // `--skip-sources` is exactly that: the covered negative keeps its id
    // and filename, and its TIFF is replaced atomically.
    private func startRun() {
        Task {
            guard await model.validateSelection() else { return }
            guard let command = model.buildRunCommand(), let rollURL = model.rollURL else { return }
            run.start(
                command: command,
                files: model.selectedFilesInCanonicalOrder,
                outputFolder: rollURL,
                totalNegatives: model.groups.count
            )
            awaitRunCompletionAndRefresh()
        }
    }

    /// A run rewrites the roll manifest while it works, so everything that
    /// reads the roll back has to re-read it once the run finishes — not just
    /// this stage's validation probe. The Edit tab in particular may never
    /// have been mounted while the run ran, so nothing else would ever tell
    /// `EditModel` the manifest changed: it keeps showing the pre-run
    /// negative list and dirty count.
    private func awaitRunCompletionAndRefresh() {
        Task {
            await run.waitForCompletion()
            model.clearValidationState()
            edit.refresh()
            library.scan()
        }
    }

    private func chooseInputFolder() {
        guard let url = ContentView.pickFolder(startingAt: model.inputFolder) else { return }
        model.inputFolder = url
    }

    private func chooseBaseFrame(replace: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = replace ? "Replace" : "Choose"
        if let nef = UTType(filenameExtension: "nef") {
            panel.allowedContentTypes = [nef]
        }
        if let inputFolder = model.inputFolder {
            panel.directoryURL = inputFolder
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await model.attachBaseFrame(at: url) }
    }

    private func chooseFlatFieldReference(replace: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = replace ? "Replace" : "Choose"
        if let nef = UTType(filenameExtension: "nef") {
            panel.allowedContentTypes = [nef]
        }
        if let inputFolder = model.inputFolder {
            panel.directoryURL = inputFolder
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await model.attachFlatFieldReference(at: url) }
    }

    /// Caption for the Convert zone while idle and scans are selected.
    nonisolated static func convertReadyLabel(scanCount: Int) -> String {
        "\(scanCount) scan\(scanCount == 1 ? "" : "s") ready to convert"
    }

    /// Given a dropped file URL and the input folder, returns the file's
    /// name if it resides inside the folder, or `nil` otherwise.
    nonisolated static func resolveFileName(_ url: URL, relativeTo inputFolder: URL) -> String? {
        let inputPath = inputFolder.standardizedFileURL.path
        let filePath = url.standardizedFileURL.path
        guard filePath.hasPrefix(inputPath) else { return nil }
        return url.lastPathComponent
    }
}

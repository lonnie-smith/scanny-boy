import AppKit
import SwiftUI

/// Import from Frame log: step 1 picks the Frame log roll, step 2 shows each
/// negative beside the frame it is paired with, editable, and Import writes
/// the pairing through `framelog apply`. Imported values replace existing
/// ones. See docs/METADATA_IMPORT_PLAN.md section 6.3.
struct FrameLogImportSheet: View {
    @Bindable var model: FrameLogImportModel
    /// The local roll's negatives by id, for the thumbnails.
    let negativesByID: [String: RollManifest.Negative]
    let cameraColor: RollManifest.CameraColor?
    let highlightLock: RollManifest.HighlightLock?

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Import from Frame log").font(.title2.bold())
            content
            footerNote
            buttons
        }
        .padding(20)
        .frame(minWidth: 720, minHeight: 520)
        .interactiveDismissDisabled(model.phase == .applying)
        .task {
            if model.rolls.isEmpty, model.phase == .choosingRoll {
                await model.loadRolls()
            }
        }
        .onChange(of: model.didImport) { _, imported in
            if imported { dismiss() }
        }
    }

    // MARK: - Content

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .choosingRoll:
            rollListStep
        case .loading:
            statusMessage("Contacting Frame log…")
        case .aligning, .applying:
            alignmentStep
                .disabled(model.phase == .applying)
        case .failed(let message):
            failureMessage(message)
        }
    }

    private func statusMessage(_ text: String) -> some View {
        VStack(spacing: 10) {
            ProgressView()
            Text(text).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func failureMessage(_ message: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle)
                .foregroundStyle(.orange)
            Text(message)
                .multilineTextAlignment(.center)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Step 1

    private var rollListStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Choose the Frame log roll for this roll’s \(model.negativeCount) negatives.")
                .foregroundStyle(.secondary)
            if model.rolls.isEmpty {
                Text("No rolls found in Frame log.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(selection: $model.selectedRollUID) {
                    ForEach(model.rolls, id: \.uid) { roll in
                        FrameLogRollRow(
                            roll: roll,
                            isLinked: roll.uid == model.linkedRollUID,
                            isLikelyMatch: model.isLikelyMatch(roll)
                        )
                        .tag(roll.uid)
                    }
                }
            }
        }
    }

    // MARK: - Step 2

    @ViewBuilder
    private var alignmentStep: some View {
        if let detail = model.detail, let alignment = model.alignment {
            VStack(alignment: .leading, spacing: 8) {
                FrameLogAlignmentHeader(roll: detail.roll, alignment: alignment)
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(alignment.rows.indices, id: \.self) { index in
                            alignmentRow(index: index, alignment: alignment)
                            Divider()
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func alignmentRow(index: Int, alignment: FrameLogAlignment) -> some View {
        let row = alignment.rows[index]
        let frame = row.frameNumber.flatMap { model.framesByNumber[$0] }
        return FrameLogAlignmentRow(
            negative: negativesByID[row.negativeID],
            cameraColor: cameraColor,
            highlightLock: highlightLock,
            position: index + 1,
            frameNumber: frameBinding(row: index),
            options: alignment.options(forRow: index),
            skipped: alignment.skipped(beforeRow: index),
            frame: frame
        )
    }

    private func frameBinding(row: Int) -> Binding<Int?> {
        Binding(
            get: { model.alignment?.rows[row].frameNumber },
            set: { newValue in
                if let newValue {
                    model.setFrame(row: row, to: newValue)
                } else {
                    model.setNoMatch(row: row)
                }
            }
        )
    }

    // MARK: - Footer

    private var footerNote: some View {
        Text("Imported values replace existing ones.")
            .font(.caption)
            .foregroundStyle(.secondary)
    }

    private var buttons: some View {
        HStack {
            switch model.phase {
            case .choosingRoll:
                Button("Reload") { Task { await model.loadRolls() } }
            case .aligning, .applying:
                Button("Back") { model.backToRolls() }
                Button("Reset Alignment") { model.resetAlignment() }
            case .loading, .failed:
                EmptyView()
            }
            if model.phase == .applying {
                ProgressView().controlSize(.small)
            }
            Spacer()
            Button("Cancel") { dismiss() }
                .keyboardShortcut(.cancelAction)
                .disabled(model.phase == .applying)
            primaryButton
        }
    }

    @ViewBuilder
    private var primaryButton: some View {
        switch model.phase {
        case .choosingRoll:
            Button("Next") {
                if let roll = model.selectedRoll {
                    Task { await model.choose(roll: roll) }
                }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(model.selectedRoll == nil)
        case .aligning, .applying:
            Button("Import") { Task { await model.apply() } }
                .keyboardShortcut(.defaultAction)
                .disabled(model.phase == .applying)
                .accessibilityIdentifier("frameLogImportButton")
        case .failed:
            Button("OK") { model.acknowledgeFailure() }
                .keyboardShortcut(.defaultAction)
        case .loading:
            EmptyView()
        }
    }
}

// MARK: - Roll row

private struct FrameLogRollRow: View {
    let roll: FrameLogRollSummary
    let isLinked: Bool
    let isLikelyMatch: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("#\(roll.rollNumber)")
                .font(.headline)
                .frame(width: 56, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(titleText)
                Text(detailText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if roll.finishedAt == nil {
                badge("active")
            }
            if isLinked {
                badge("Linked")
            }
            if isLikelyMatch {
                Text("likely match")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    private var titleText: String {
        let parts = [roll.film, roll.camera].compactMap { $0 }
        return parts.isEmpty ? "Untitled roll" : parts.joined(separator: " · ")
    }

    private var detailText: String {
        var parts: [String] = []
        if let first = roll.firstCapturedAt {
            let start = String(first.prefix(10))
            if let last = roll.lastCapturedAt, String(last.prefix(10)) != start {
                parts.append("\(start) – \(last.prefix(10))")
            } else {
                parts.append(start)
            }
        }
        parts.append(roll.frameCount == 1 ? "1 frame" : "\(roll.frameCount) frames")
        return parts.joined(separator: " · ")
    }

    private func badge(_ text: String) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(.quaternary, in: Capsule())
    }
}

// MARK: - Alignment header

private struct FrameLogAlignmentHeader: View {
    let roll: FrameLogRollSummary
    let alignment: FrameLogAlignment

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(titleText).font(.headline)
            Text(countsText)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var titleText: String {
        let parts = [roll.film, roll.camera].compactMap { $0 }
        let name = parts.isEmpty ? "" : " · " + parts.joined(separator: " · ")
        return "Roll #\(roll.rollNumber)\(name)"
    }

    private var countsText: String {
        "\(alignment.matchedCount) of \(alignment.rows.count) negatives matched · "
            + "\(alignment.unusedFrameNumbers.count) of \(alignment.lastFrame) frames unused"
    }
}

// MARK: - Alignment row

private struct FrameLogAlignmentRow: View {
    let negative: RollManifest.Negative?
    let cameraColor: RollManifest.CameraColor?
    let highlightLock: RollManifest.HighlightLock?
    let position: Int
    @Binding var frameNumber: Int?
    let options: [Int]
    let skipped: Int
    let frame: FrameLogFrame?

    var body: some View {
        HStack(spacing: 12) {
            FrameLogNegativeThumbnail(
                negative: negative,
                cameraColor: cameraColor,
                highlightLock: highlightLock
            )
            Text("\(negative?.sequence ?? position)")
                .font(.headline.monospacedDigit())
                .frame(width: 32, alignment: .trailing)
            picker
            summary
            Spacer(minLength: 0)
            if skipped > 0 {
                Text("⇣ \(skipped) skipped")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 6)
    }

    private var picker: some View {
        Picker("Frame", selection: $frameNumber) {
            Text("No match").tag(Int?.none)
            ForEach(options, id: \.self) { number in
                Text("Frame \(number)").tag(Int?.some(number))
            }
        }
        .labelsHidden()
        .frame(width: 120)
    }

    @ViewBuilder
    private var summary: some View {
        if frameNumber == nil {
            Text("No match — left unchanged")
                .foregroundStyle(.secondary)
        } else if let text = Self.summaryText(for: frame) {
            HStack(spacing: 4) {
                Text(text).lineLimit(2)
                if frame?.hasPosition == true {
                    Image(systemName: "location.fill")
                        .foregroundStyle(.secondary)
                }
            }
        } else {
            Text("No metadata in log")
                .foregroundStyle(.secondary)
        }
    }

    /// Local time or date, lens, location name and a caption snippet, or
    /// `nil` when the frame carries none of them.
    static func summaryText(for frame: FrameLogFrame?) -> String? {
        guard let frame else { return nil }
        var parts: [String] = []
        if let local = frame.capturedLocal {
            if frame.precision == "date" {
                parts.append(String(local.prefix(10)))
            } else {
                parts.append(String(local.prefix(16)).replacingOccurrences(of: "T", with: " "))
            }
        }
        if let lens = frame.lens { parts.append(lens) }
        if let place = frame.locationName { parts.append(place) }
        if let caption = frame.caption { parts.append(String(caption.prefix(40))) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

// MARK: - Thumbnail

/// The filmstrip's thumbnail, without its selection button: the same
/// `ThumbnailLoader` call and cache generation as `FilmstripCell`.
private struct FrameLogNegativeThumbnail: View {
    let negative: RollManifest.Negative?
    let cameraColor: RollManifest.CameraColor?
    let highlightLock: RollManifest.HighlightLock?

    @Environment(\.displayScale) private var displayScale
    @State private var thumbnail: Thumbnail?

    private static let size = CGSize(width: 56, height: 56)

    var body: some View {
        thumbnailContent
            .frame(width: Self.size.width, height: Self.size.height)
            .clipShape(RoundedRectangle(cornerRadius: 4))
            .task(id: taskID) {
                thumbnail = nil
                guard let negative, let previewPath = negative.previewPath else { return }
                thumbnail = await ThumbnailLoader.shared.thumbnail(
                    forPreview: URL(filePath: previewPath),
                    generation: EditModel.renderGeneration(
                        of: negative, cameraColor: cameraColor, highlightLock: highlightLock
                    ),
                    pointSize: Self.size,
                    scale: displayScale
                )
            }
    }

    private var taskID: String {
        guard let negative else { return "none" }
        let generation = EditModel.renderGeneration(
            of: negative, cameraColor: cameraColor, highlightLock: highlightLock
        )
        return "\(negative.previewPath ?? "none")#\(generation)"
    }

    @ViewBuilder
    private var thumbnailContent: some View {
        if let thumbnail {
            Image(nsImage: thumbnail.image)
                .resizable()
                .interpolation(.medium)
                .aspectRatio(contentMode: .fit)
        } else {
            RoundedRectangle(cornerRadius: 4)
                .fill(.quaternary)
                .overlay {
                    Image(systemName: "photo")
                        .foregroundStyle(.secondary)
                }
        }
    }
}

// MARK: - Presentation

/// Presents `FrameLogImportSheet` on the open roll when the File menu or the
/// Metadata stage asks for it. A modifier of its own because
/// `ContentView.body` is at the type checker's limit.
struct FrameLogImportPresentation: ViewModifier {
    let edit: EditModel
    let keyboard: AppKeyboardState
    /// Whether no other sheet is up; supplied by `ContentView`.
    let canPresent: () -> Bool

    @State private var importModel: FrameLogImportModel?
    @State private var isPresented = false

    func body(content: Content) -> some View {
        content
            .onAppear(perform: refreshToken)
            .onReceive(
                NotificationCenter.default.publisher(for: NSWindow.didBecomeKeyNotification)
            ) { _ in
                refreshToken()
            }
            .onReceive(
                NotificationCenter.default.publisher(for: .scannyBoyRequestFrameLogImport)
            ) { _ in
                present()
            }
            .sheet(isPresented: $isPresented, onDismiss: { importModel = nil }) {
                sheetContent
            }
    }

    @ViewBuilder
    private var sheetContent: some View {
        if let importModel {
            FrameLogImportSheet(
                model: importModel,
                negativesByID: Dictionary(
                    (edit.roll?.negatives ?? []).map { ($0.negativeID, $0) },
                    uniquingKeysWith: { first, _ in first }
                ),
                cameraColor: edit.roll?.cameraColor,
                highlightLock: edit.roll?.highlightLock
            )
        }
    }

    @MainActor
    private func refreshToken() {
        keyboard.hasFrameLogToken = AppEnvironment.frameLogTokenStore.token() != nil
    }

    /// Guarded here as well as by the enablement of the menu item and the
    /// button, which SwiftUI can leave stale until the menu reopens.
    @MainActor
    private func present() {
        refreshToken()
        guard !isPresented,
            keyboard.canImportFromFrameLog,
            canPresent(),
            let rollURL = edit.rollURL,
            let roll = edit.roll
        else { return }
        importModel = FrameLogImportModel(
            runner: edit.runner,
            rollURL: rollURL,
            negativeCount: roll.negatives.filter { $0.sequence != nil }.count,
            linkedRollUID: roll.metadata.framelogRollUID,
            onImported: { [edit] manifest in
                await edit.adoptImportedManifest(manifest, for: rollURL)
            }
        )
        isPresented = true
    }
}

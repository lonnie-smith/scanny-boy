import Foundation
import Observation

/// The Frame log import sheet's state: the roll list, the chosen roll's
/// frames, the editable alignment, and the apply. Every command runs
/// one-shot through the CLI with the token in its environment, never in
/// argv. See docs/METADATA_IMPORT_PLAN.md section 6.2.
@MainActor
@Observable
final class FrameLogImportModel {
    enum Phase: Sendable, Equatable {
        case choosingRoll
        case loading
        case aligning
        case applying
        case failed(String)
    }

    static let noTokenMessage = "Save a Frame log token in Settings first."

    /// A roll whose frame count is within this many of the negative count
    /// is marked as a likely match.
    static let likelyMatchTolerance = 3

    let runner: CLIRunner
    /// The local roll folder the import writes to.
    let rollURL: URL
    /// How many sequenced negatives the local roll has.
    let negativeCount: Int
    /// The local roll's current `framelog_roll_uid`, if linked.
    let linkedRollUID: String?

    private(set) var phase: Phase = .choosingRoll
    private(set) var rolls: [FrameLogRollSummary] = []
    /// The highlighted row of the roll list.
    var selectedRollUID: String?
    private(set) var detail: FrameLogRollDetail?
    private(set) var alignment: FrameLogAlignment?
    private(set) var framesByNumber: [Int: FrameLogFrame] = [:]
    /// Set once the import has landed and the manifest was handed over; the
    /// sheet dismisses itself on it.
    private(set) var didImport = false

    private let tokens: any FrameLogTokenStore
    private let settings: FrameLogSettings
    private let onImported: @MainActor @Sendable (RollManifest) async -> Void

    init(
        runner: CLIRunner,
        rollURL: URL,
        negativeCount: Int,
        linkedRollUID: String?,
        tokens: any FrameLogTokenStore = AppEnvironment.frameLogTokenStore,
        settings: FrameLogSettings = FrameLogSettings(),
        onImported: @escaping @MainActor @Sendable (RollManifest) async -> Void = { _ in }
    ) {
        self.runner = runner
        self.rollURL = rollURL
        self.negativeCount = negativeCount
        self.linkedRollUID = linkedRollUID
        self.tokens = tokens
        self.settings = settings
        self.onImported = onImported
        selectedRollUID = linkedRollUID
    }

    // MARK: - Derived values

    func isLikelyMatch(_ roll: FrameLogRollSummary) -> Bool {
        abs(roll.frameCount - negativeCount) <= Self.likelyMatchTolerance
    }

    var selectedRoll: FrameLogRollSummary? {
        rolls.first { $0.uid == selectedRollUID }
    }

    // MARK: - Step 1: the roll list

    /// Runs `framelog rolls` and shows the list, with the linked roll
    /// preselected when the API returned it.
    func loadRolls() async {
        guard let token = tokens.token() else {
            phase = .failed(Self.noTokenMessage)
            return
        }
        phase = .loading
        let command = CLICommand
            .framelogRolls(status: "all", baseURL: settings.baseURL)
            .withEnvironment([FrameLogCredentials.tokenEnvironmentKey: token])
        let result = await Self.run(runner: runner, command: command)
        guard result.succeeded, let loaded = result.rolls else {
            phase = .failed(result.errorMessage ?? "The Frame log request failed.")
            return
        }
        rolls = loaded.sorted { $0.rollNumber > $1.rollNumber }
        if selectedRoll == nil {
            selectedRollUID = nil
            if let linkedRollUID, rolls.contains(where: { $0.uid == linkedRollUID }) {
                selectedRollUID = linkedRollUID
            }
        }
        phase = .choosingRoll
    }

    /// Runs `framelog roll` for `roll` and moves to the alignment step.
    func choose(roll: FrameLogRollSummary) async {
        guard let token = tokens.token() else {
            phase = .failed(Self.noTokenMessage)
            return
        }
        selectedRollUID = roll.uid
        phase = .loading
        let command = CLICommand
            .framelogRoll(roll: rollURL, ref: roll.uid, baseURL: settings.baseURL)
            .withEnvironment([FrameLogCredentials.tokenEnvironmentKey: token])
        let result = await Self.run(runner: runner, command: command)
        guard result.succeeded, let loaded = result.detail else {
            phase = .failed(result.errorMessage ?? "The Frame log request failed.")
            return
        }
        detail = loaded
        alignment = FrameLogAlignment(entries: loaded.alignment, lastFrame: loaded.roll.lastFrame)
        framesByNumber = Dictionary(
            loaded.frames.map { ($0.frameNumber, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        phase = .aligning
    }

    // MARK: - Step 2: the alignment

    func setFrame(row: Int, to frame: Int) {
        alignment?.setFrame(row: row, to: frame)
    }

    func setNoMatch(row: Int) {
        alignment?.setNoMatch(row: row)
    }

    func resetAlignment() {
        alignment?.reset()
    }

    /// Back from the alignment to the roll list.
    func backToRolls() {
        guard phase == .aligning else { return }
        phase = .choosingRoll
    }

    /// Leaves the failed phase for the step the failure came from.
    func acknowledgeFailure() {
        guard case .failed = phase else { return }
        phase = alignment == nil ? .choosingRoll : .aligning
    }

    /// Runs `framelog apply` with every row's assignment. On success the
    /// returned manifest goes to `onImported` and `didImport` is set.
    func apply() async {
        guard phase == .aligning, let detail, let alignment else { return }
        guard let token = tokens.token() else {
            phase = .failed(Self.noTokenMessage)
            return
        }
        phase = .applying
        let command = CLICommand
            .framelogApply(
                roll: rollURL,
                ref: detail.roll.uid,
                assignments: alignment.assignments,
                baseURL: settings.baseURL
            )
            .withEnvironment([FrameLogCredentials.tokenEnvironmentKey: token])
        let result = await Self.run(runner: runner, command: command)
        guard result.succeeded, let manifest = result.manifest else {
            phase = .failed(result.errorMessage ?? "The Frame log import did not complete.")
            return
        }
        await onImported(manifest)
        didImport = true
    }

    // MARK: - Running a command

    private struct CommandResult: Sendable {
        var rolls: [FrameLogRollSummary]?
        var detail: FrameLogRollDetail?
        var manifest: RollManifest?
        var errorMessage: String?
        var succeeded = false
    }

    private static func run(runner: CLIRunner, command: CLICommand) async -> CommandResult {
        var result = CommandResult()
        do {
            for await output in try await runner.session(for: command).start() {
                switch output {
                case .event(let event):
                    switch event.kind {
                    case .framelogRolls:
                        result.rolls = event.frameLogRolls
                    case .framelogRoll:
                        result.detail = event.frameLogRoll
                    case .metadataUpdated:
                        if let fields = event.manifest {
                            result.manifest = RollManifest(fields: fields)
                        }
                    case .error:
                        result.errorMessage = event.message ?? "The Frame log request failed."
                    default:
                        continue
                    }
                case .completed(let completion):
                    result.succeeded = completion.outcome == .success
                case .log, .failure:
                    continue
                }
            }
        } catch {
            result.errorMessage = error.localizedDescription
        }
        return result
    }
}

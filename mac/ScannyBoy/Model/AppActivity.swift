import Foundation
import Observation

/// Section 3.10's "one helper at a time" is an app-wide invariant, but
/// `RunModel`, `EditModel`, `ExportModel`, `RigModel`, and
/// `ConfigurationModel` each drive their own `CLISession` and track their
/// own busy flag. This is the single derived source of truth every view
/// gates on instead of reading `run.isActive` alone, which only ever
/// covered `RunModel`'s own helper.
///
/// Tethered capture relaxes the rule: the Capture tab stays live while a
/// roll's stitch queue drains (docs/TETHER_PLAN.md §4.2).
@MainActor
@Observable
final class AppActivity {
    private let run: RunModel
    private let edit: EditModel
    private let export: ExportModel
    private let rig: RigModel
    private let configuration: ConfigurationModel
    private let capture: CaptureSessionModel
    private let stitchQueue: StitchQueueModel

    init(
        run: RunModel,
        edit: EditModel,
        export: ExportModel,
        rig: RigModel,
        configuration: ConfigurationModel,
        capture: CaptureSessionModel,
        stitchQueue: StitchQueueModel
    ) {
        self.run = run
        self.edit = edit
        self.export = export
        self.rig = rig
        self.configuration = configuration
        self.capture = capture
        self.stitchQueue = stitchQueue
    }

    /// Global busy: blocks most workspace chrome, but not the Capture tab.
    var isBusy: Bool {
        run.isActive
            || edit.isRotating
            || edit.isDeleting
            || edit.isDetectingScratches
            || edit.isTogglingScratches
            || edit.isDetectingSpots
            || edit.isReviewingSpots
            || export.isExporting
            || rig.isCreating
            || configuration.isAttachingBaseFrame
            || configuration.isAttachingFlatFieldReference
            || configuration.isSettingFilmKind
            || configuration.isValidating
    }

    /// Whether roll-writing controls should be disabled for this roll.
    func isRollWriteLocked(for rollURL: URL?) -> Bool {
        guard let rollURL else { return isBusy }
        if capture.isSessionOpen, capture.rollURL == rollURL { return true }
        if stitchQueue.hasWork, stitchQueue.rollURL == rollURL { return true }
        return isBusy
    }

    /// Whether the sidebar should refuse roll selection changes.
    var isSidebarSelectionLocked: Bool {
        if capture.isSessionOpen { return true }
        if stitchQueue.hasWork { return true }
        return isBusy
    }
}

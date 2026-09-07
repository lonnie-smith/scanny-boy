import Observation

/// Section 3.10's "one helper at a time" is an app-wide invariant, but
/// `RunModel`, `EditModel`, `ExportModel`, `FlatFieldModel`, and
/// `ConfigurationModel` each drive their own `CLISession` and track their
/// own busy flag. This is the single derived source of truth every view
/// gates on instead of reading `run.isActive` alone, which only ever
/// covered `RunModel`'s own helper.
@MainActor
@Observable
final class AppActivity {
    private let run: RunModel
    private let edit: EditModel
    private let export: ExportModel
    private let flatField: FlatFieldModel
    private let configuration: ConfigurationModel

    init(
        run: RunModel,
        edit: EditModel,
        export: ExportModel,
        flatField: FlatFieldModel,
        configuration: ConfigurationModel
    ) {
        self.run = run
        self.edit = edit
        self.export = export
        self.flatField = flatField
        self.configuration = configuration
    }

    var isBusy: Bool {
        run.isActive
            || edit.isRotating
            || edit.isDeleting
            || export.isExporting
            || flatField.isCreating
            || configuration.isAttachingBaseFrame
            || configuration.isSettingFilmKind
            || configuration.isValidating
    }
}

import SwiftUI

/// The Heal tab's "Bands" section (docs/DEBAND_PLAN.md §5.2): removes broad,
/// low-contrast colour bands running along the film's length from regions
/// the user draws over flat banded film. Everything acts on the *displayed*
/// negative — a region's position belongs to one frame, so unlike
/// scratches and spots there is no selection fan-out.
///
/// The panel only reads `negative.debandSummary` (the CLI's display-space
/// report) and sends commands; the region list's hover feeds
/// `edit.hoveredDebandRegionID`, which the preview's outline layer reads.
struct DebandPanel: View {
    let negative: RollManifest.Negative
    @Bindable var edit: EditModel
    /// The draw-mode session — a `CropSession` the preview pane owns, driven
    /// by the overlay; the panel supplies the Add / Cancel buttons.
    @Bindable var drawSession: CropSession
    let isMonochromeRoll: Bool
    let runIsActive: Bool
    let onBeginDraw: () -> Void
    let onAddDrawnRegion: () -> Void

    /// The slider's working value: seeded from the report, committed to the
    /// CLI on release.
    @State private var strength = NegativeDeband.Summary.defaultStrength

    private var summary: NegativeDeband.Summary? { negative.debandSummary }
    private var regions: [NegativeDeband.Region] { summary?.regions ?? [] }

    private var isBusy: Bool {
        edit.isDebanding || edit.isRotating || edit.isDeleting || edit.isCropping
            || runIsActive
    }

    /// The controls that edit an existing op: off on a mono roll, while a
    /// round trip is in flight, and while a region is being drawn.
    private var controlsDisabled: Bool {
        isMonochromeRoll || isBusy || drawSession.isActive
    }

    /// What a fresh op would run along: the TIFF's vertical axis, which a
    /// quarter turn displays as horizontal. Only the picker's fallback
    /// while no op exists; once one does, the report's axis is the truth.
    private var defaultAxis: NegativeDeband.Axis {
        negative.rotationQuarterTurns % 2 == 0 ? .vertical : .horizontal
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Bands")
                    .font(.headline)
                Spacer()
                if edit.isDebanding {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            Toggle("Remove bands", isOn: enabledBinding)
                .disabled(controlsDisabled || regions.isEmpty || summary?.stale == true)

            Picker("Bands run", selection: axisBinding) {
                Text("↕ Vertical").tag(NegativeDeband.Axis.vertical)
                Text("↔ Horizontal").tag(NegativeDeband.Axis.horizontal)
            }
            .pickerStyle(.segmented)
            .disabled(controlsDisabled)
            .help("The direction the bands run on the image as it is displayed")

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Strength")
                    Spacer()
                    Text(String(format: "%.2f", strength))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                Slider(
                    value: $strength,
                    in: NegativeDeband.Summary.strengthRange,
                    step: 0.05
                ) { editing in
                    if !editing { commitStrength() }
                }
                .doubleClickReset {
                    strength = NegativeDeband.Summary.defaultStrength
                    commitStrength()
                }
                .disabled(controlsDisabled || regions.isEmpty)
                .accessibilityLabel("Band removal strength")
            }

            if !regions.isEmpty {
                regionList
            }

            drawControls

            captionRow
        }
        .onAppear(perform: syncStrength)
        .onChange(of: summary?.strength) { syncStrength() }
        // A refused commit leaves the slider where the user dropped it.
        .onChange(of: edit.debandFailure) { syncStrength() }
        .onChange(of: negative.negativeID) {
            edit.hoveredDebandRegionID = nil
        }
        .onDisappear { edit.hoveredDebandRegionID = nil }
    }

    // MARK: - Regions

    private var regionList: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(regions) { region in
                HStack {
                    Text(region.title)
                        .font(.callout)
                        .monospacedDigit()
                    Spacer()
                    Button {
                        Task { await edit.removeDebandRegion(negative, id: region.id) }
                    } label: {
                        Image(systemName: "minus.circle")
                    }
                    .buttonStyle(.borderless)
                    .disabled(controlsDisabled)
                    .help("Remove this region (the others are refitted)")
                    .accessibilityLabel("Remove region \(region.id)")
                }
                .padding(.vertical, 2)
                .contentShape(Rectangle())
                .onHover { hovering in
                    if hovering {
                        edit.hoveredDebandRegionID = region.id
                    } else if edit.hoveredDebandRegionID == region.id {
                        edit.hoveredDebandRegionID = nil
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var drawControls: some View {
        if drawSession.isActive {
            Text("Drag over a flat, banded area of film, then adjust with the handles.")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Button("Add") { onAddDrawnRegion() }
                    .disabled(edit.isDebanding)
                    .keyboardShortcut(.defaultAction)
                    .help("Fit and add this region (Return)")
                Button("Cancel") { drawSession.end() }
                    .keyboardShortcut(.cancelAction)
                    .help("Discard this region (Escape)")
            }
        } else {
            HStack {
                Button {
                    onBeginDraw()
                } label: {
                    Label("Add Region", systemImage: "plus.rectangle.dashed")
                }
                .disabled(isMonochromeRoll || isBusy || negative.output == nil)
                .help("Draw a region over flat banded film")
                Spacer()
                if !regions.isEmpty {
                    Button("Clear", role: .destructive) {
                        Task { await edit.clearDeband(negative) }
                    }
                    .disabled(controlsDisabled)
                    .help("Remove every region (the strength setting stays)")
                }
            }
        }
    }

    // MARK: - Caption

    /// One caption at a time: the colour-film note, the last failure, or
    /// the stale note.
    @ViewBuilder
    private var captionRow: some View {
        if isMonochromeRoll {
            caption("Colour film only")
        } else if let failure = edit.debandFailure {
            caption(failure)
        } else if summary?.stale == true {
            caption(
                "Stale — the negative was re-stitched and these regions could not be "
                    + "refitted. Add or remove a region to refit them."
            )
        } else if regions.isEmpty, !drawSession.isActive {
            caption("Draw a region over flat film showing bands.")
        }
    }

    private func caption(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Bindings

    private var enabledBinding: Binding<Bool> {
        Binding(
            get: { summary?.enabled ?? false },
            set: { newValue in
                guard newValue != (summary?.enabled ?? false) else { return }
                Task { await edit.setDeband(negative, enabled: newValue) }
            }
        )
    }

    private var axisBinding: Binding<NegativeDeband.Axis> {
        Binding(
            get: { summary?.axis ?? defaultAxis },
            set: { newValue in
                guard newValue != (summary?.axis ?? defaultAxis) else { return }
                Task { await edit.setDebandAxis(negative, newValue) }
            }
        )
    }

    private func syncStrength() {
        strength = summary?.strength ?? NegativeDeband.Summary.defaultStrength
    }

    /// Commits the slider's value on release (or on a double-click reset),
    /// when it differs from what the CLI last reported.
    private func commitStrength() {
        let reported = summary?.strength ?? NegativeDeband.Summary.defaultStrength
        guard abs(strength - reported) > 1e-9 else { return }
        let value = strength
        Task { await edit.setDebandStrength(negative, value) }
    }
}

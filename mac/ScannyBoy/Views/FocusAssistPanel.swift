import SwiftUI

/// Loupe, zoom readout, focus meter, and check-shot grid.
struct FocusAssistPanel: View {
    @Bindable var focusAssist: FocusAssistModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let error = focusAssist.liveViewError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.orange)
                if focusAssist.canRetryLiveView {
                    Button("Retry") { focusAssist.retryLiveView() }
                }
            } else {
                HStack(alignment: .top, spacing: 16) {
                    loupe
                    VStack(alignment: .leading, spacing: 8) {
                        zoomSection
                        areaOutline
                        meterSection
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if !focusAssist.checkShotCells.isEmpty {
                checkShotGrid
            }
            if let diagnosis = focusAssist.checkShotDiagnosis {
                Text(diagnosis)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if focusAssist.isCheckShotBusy {
                ProgressView("Check shot…")
            }
            Text("F or Esc closes · R resets peak · C check shot")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private var loupe: some View {
        Group {
            if let image = focusAssist.loupeImage {
                Image(decorative: image, scale: 2, orientation: .up)
                    .interpolation(.none)
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: 320, maxHeight: 220)
                    .background(Color.black)
            } else {
                Rectangle()
                    .fill(Color.black.opacity(0.85))
                    .frame(width: 320, height: 212)
                    .overlay {
                        ProgressView()
                            .tint(.white)
                    }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 4))
    }

    @ViewBuilder
    private var zoomSection: some View {
        Text(focusAssist.zoomText)
            .font(.callout.monospacedDigit())
        if let hint = focusAssist.zoomHint {
            Text(hint)
                .font(.caption)
                .foregroundStyle(.orange)
        }
    }

    @ViewBuilder
    private var areaOutline: some View {
        let sensorWidth = CGFloat(FocusAssistTuning.sensorWidth)
        let sensorHeight = CGFloat(FocusAssistTuning.sensorHeight)
        let areaWidth = CGFloat(focusAssist.areaSize.width)
        let areaHeight = CGFloat(focusAssist.areaSize.height)
        let centreX = CGFloat(focusAssist.areaCentre.x)
        let centreY = CGFloat(focusAssist.areaCentre.y)
        let rectX = (centreX - areaWidth / 2) / sensorWidth
        let rectY = (centreY - areaHeight / 2) / sensorHeight
        let rectW = max(0.05, areaWidth / sensorWidth)
        let rectH = max(0.05, areaHeight / sensorHeight)

        Canvas { context, size in
            let outline = CGRect(origin: .zero, size: size)
            context.stroke(
                Path(roundedRect: outline.insetBy(dx: 0.5, dy: 0.5), cornerRadius: 2),
                with: .color(.secondary),
                lineWidth: 1
            )
            let inner = CGRect(
                x: rectX * size.width,
                y: rectY * size.height,
                width: rectW * size.width,
                height: rectH * size.height
            )
            context.stroke(
                Path(roundedRect: inner, cornerRadius: 1),
                with: .color(.primary),
                lineWidth: 1.5
            )
        }
        .frame(width: 80, height: 54)
        .accessibilityLabel("Live view area on sensor")
    }

    @ViewBuilder
    private var meterSection: some View {
        if let message = focusAssist.meterMessage {
            Text(message)
                .font(.callout)
                .foregroundStyle(.secondary)
        } else if let percentage = focusAssist.meterPercentage {
            VStack(alignment: .leading, spacing: 4) {
                GeometryReader { geometry in
                    ZStack(alignment: .leading) {
                        Capsule()
                            .fill(Color.secondary.opacity(0.2))
                        Capsule()
                            .fill(percentage >= FocusAssistTuning.peakBand * 100 ? Color.green : Color.accentColor)
                            .frame(width: geometry.size.width * percentage / 100)
                        Rectangle()
                            .fill(Color.primary)
                            .frame(width: 2)
                            .offset(x: geometry.size.width - 2)
                    }
                }
                .frame(height: 12)
                Text(String(format: "%.0f%%", percentage))
                    .font(.title2.monospacedDigit())
            }
        }
    }

    @ViewBuilder
    private var checkShotGrid: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Check shot")
                .font(.caption.weight(.semibold))
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 3), spacing: 4) {
                ForEach(focusAssist.checkShotCells) { cell in
                    checkShotCell(cell)
                }
            }
            .frame(maxWidth: 180)
        }
    }

    @ViewBuilder
    private func checkShotCell(_ cell: FocusCheckCell) -> some View {
        let label = cell.percentage.map { String(format: "%.0f", $0) } ?? "—"
        Text(label)
            .font(.caption.monospacedDigit())
            .frame(maxWidth: .infinity, minHeight: 28)
            .background(
                RoundedRectangle(cornerRadius: 3)
                    .fill(cell.percentage == nil ? Color.secondary.opacity(0.15) : Color.accentColor.opacity(0.2))
            )
    }
}

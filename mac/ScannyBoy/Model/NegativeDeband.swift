import CoreGraphics
import Foundation

/// A negative's net development-band removal, as `roll info`'s per-negative
/// `deband` block and every `edit_recorded`'s `deband` field report it
/// (protocol version 25, docs/DEBAND_PLAN.md). Everything here is display
/// space — the image as it renders, live crop included — and the CLI has
/// already transformed it: a rotation or flip moves the rects and swaps the
/// axis, which is why the report rides every edit confirmation. The app
/// converts no coordinates and never sees a correction table.
struct NegativeDeband: Sendable, Hashable {
    /// The direction the bands run, as displayed.
    enum Axis: String, Sendable, Hashable, CaseIterable {
        case vertical
        case horizontal
    }

    /// One region the user drew: a stable id and the bounding box of the
    /// (possibly tilted) region on the display image.
    struct Region: Sendable, Hashable, Identifiable {
        let id: Int
        let displayRect: CGRect

        /// "Region 1 · 7970 × 1092" — the display-space size.
        var title: String {
            "Region \(id) · \(Int(displayRect.width)) × \(Int(displayRect.height))"
        }

        init(id: Int, displayRect: CGRect) {
            self.id = id
            self.displayRect = displayRect
        }

        init?(fields: [String: JSONValue]) {
            guard
                let id = fields["id"]?.intValue,
                let rect = fields["display_rect"]?.arrayValue,
                rect.count == 4
            else { return nil }
            let values = rect.compactMap(\.doubleValue)
            guard values.count == 4 else { return nil }
            self.init(
                id: id,
                displayRect: CGRect(
                    x: values[0], y: values[1], width: values[2], height: values[3]
                )
            )
        }
    }

    /// The report: `{fit_version, enabled, strength, axis, regions, stale}`.
    struct Summary: Sendable, Hashable {
        /// The slider's range and the neutral value `edit deband --strength`
        /// takes.
        static let strengthRange: ClosedRange<Double> = 0...1.5
        static let defaultStrength = 1.0

        let fitVersion: Int
        let enabled: Bool
        let strength: Double
        let axis: Axis
        let regions: [Region]
        /// The op was fitted against a canvas a re-stitch replaced and could
        /// not be refitted; it applies nothing until the user redraws.
        let stale: Bool

        init(
            fitVersion: Int = 1,
            enabled: Bool,
            strength: Double,
            axis: Axis,
            regions: [Region],
            stale: Bool = false
        ) {
            self.fitVersion = fitVersion
            self.enabled = enabled
            self.strength = strength
            self.axis = axis
            self.regions = regions
            self.stale = stale
        }

        init?(fields: [String: JSONValue]) {
            guard
                let fitVersion = fields["fit_version"]?.intValue,
                let enabled = fields["enabled"]?.boolValue,
                let strength = fields["strength"]?.doubleValue,
                let axis = fields["axis"]?.stringValue.flatMap(Axis.init(rawValue:)),
                let regions = fields["regions"]?.arrayValue,
                let stale = fields["stale"]?.boolValue
            else { return nil }
            let parsed = regions.compactMap { $0.objectValue.flatMap(Region.init(fields:)) }
            // A region the app cannot read is a report it cannot trust.
            guard parsed.count == regions.count else { return nil }
            self.init(
                fitVersion: fitVersion,
                enabled: enabled,
                strength: strength,
                axis: axis,
                regions: parsed,
                stale: stale
            )
        }
    }
}

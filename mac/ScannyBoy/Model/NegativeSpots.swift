import Foundation

/// The Edit tab's spot-review state (protocol version 13, SPOTTING_PLAN §8).
///
/// Two shapes ride the wire and they are deliberately separate: the full
/// spot list comes from `edit list-spots` (and the recording commands'
/// `spots_reported` events) for the *displayed* negative only, and a
/// per-negative summary rides `roll info` so a 36-negative roll never
/// streams its whole spot state on every roll switch.
///
/// Every rect here is **display space**, straight from the CLI — Swift
/// converts no coordinates, and rejection is by `id`, never by position.
/// The RLE masks never leave the ops log.
struct NegativeSpots: Sendable, Hashable {
    struct Spot: Sendable, Hashable, Identifiable {
        let id: Int
        /// `"blob"` or `"streak"`.
        let kind: String
        /// `"dense"` (crud blocking light — the print reads locally white)
        /// or `"thin"` (a scratch through the emulsion — the print reads
        /// locally black).
        let polarity: String
        /// Display space, straight from the CLI.
        let rect: CGRect
        let score: Double
        let rejected: Bool
    }

    /// `roll info`'s per-negative block: the counts, without the list —
    /// the full list is `edit list-spots`' job.
    struct Summary: Sendable, Hashable {
        let detectorVersion: Int
        let sensitivity: Double
        let repair: Bool
        /// True when the set was detected against a canvas a re-stitch has
        /// replaced (SPOTTING_PLAN §1.5): it repairs nothing and needs
        /// re-detecting.
        let stale: Bool
        let count: Int
        let rejected: Int

        init(
            detectorVersion: Int,
            sensitivity: Double,
            repair: Bool,
            stale: Bool,
            count: Int,
            rejected: Int
        ) {
            self.detectorVersion = detectorVersion
            self.sensitivity = sensitivity
            self.repair = repair
            self.stale = stale
            self.count = count
            self.rejected = rejected
        }

        init?(fields: [String: JSONValue]) {
            guard
                let detectorVersion = fields["detector_version"]?.intValue,
                let sensitivity = fields["sensitivity"]?.doubleValue,
                let repair = fields["repair"]?.boolValue,
                let stale = fields["stale"]?.boolValue,
                let count = fields["count"]?.intValue,
                let rejected = fields["rejected"]?.intValue
            else { return nil }
            self.detectorVersion = detectorVersion
            self.sensitivity = sensitivity
            self.repair = repair
            self.stale = stale
            self.count = count
            self.rejected = rejected
        }
    }

    let detectorVersion: Int
    let sensitivity: Double
    let repair: Bool
    let spots: [Spot]
    /// The detector's count before the 500-spot cap.
    let found: Int

    var accepted: [Spot] { spots.filter { !$0.rejected } }

    init(
        detectorVersion: Int,
        sensitivity: Double,
        repair: Bool,
        spots: [Spot],
        found: Int
    ) {
        self.detectorVersion = detectorVersion
        self.sensitivity = sensitivity
        self.repair = repair
        self.spots = spots
        self.found = found
    }

    init?(event: CLIEvent) {
        guard
            let detectorVersion = event.fields["detector_version"]?.intValue,
            let sensitivity = event.fields["sensitivity"]?.doubleValue,
            let repair = event.fields["repair"]?.boolValue,
            let found = event.fields["found"]?.intValue
        else { return nil }
        self.detectorVersion = detectorVersion
        self.sensitivity = sensitivity
        self.repair = repair
        self.found = found
        self.spots = (event.fields["spots"]?.arrayValue ?? []).compactMap {
            guard let fields = $0.objectValue,
                let id = fields["id"]?.intValue,
                let kind = fields["kind"]?.stringValue,
                let polarity = fields["polarity"]?.stringValue,
                let rectFields = fields["rect"]?.arrayValue,
                rectFields.count == 4,
                let x = rectFields[0].doubleValue,
                let y = rectFields[1].doubleValue,
                let width = rectFields[2].doubleValue,
                let height = rectFields[3].doubleValue
            else { return nil }
            return Spot(
                id: id,
                kind: kind,
                polarity: polarity,
                rect: CGRect(x: x, y: y, width: width, height: height),
                score: fields["score"]?.doubleValue ?? 0,
                rejected: fields["rejected"]?.boolValue ?? false
            )
        }
    }
}

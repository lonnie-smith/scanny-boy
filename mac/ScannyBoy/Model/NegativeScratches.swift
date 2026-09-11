import Foundation

/// Per-negative scratch-removal summary from `roll info`.
struct NegativeScratches: Sendable, Hashable {
    struct Summary: Sendable, Hashable {
        let detectorVersion: Int
        let enabled: Bool
        let stale: Bool
        let count: Int

        init(detectorVersion: Int, enabled: Bool, stale: Bool, count: Int) {
            self.detectorVersion = detectorVersion
            self.enabled = enabled
            self.stale = stale
            self.count = count
        }

        init?(fields: [String: JSONValue]) {
            guard
                let detectorVersion = fields["detector_version"]?.intValue,
                let enabled = fields["enabled"]?.boolValue,
                let stale = fields["stale"]?.boolValue,
                let count = fields["count"]?.intValue
            else { return nil }
            self.detectorVersion = detectorVersion
            self.enabled = enabled
            self.stale = stale
            self.count = count
        }
    }
}

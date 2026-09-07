import Foundation

/// One grid configuration preset, as `grid list` and `grid_created` report
/// it — a user label for an `across` x `down` grouping shape.
struct GridProfile: Identifiable, Sendable, Hashable {
    let profileID: String
    let name: String
    let across: Int
    let down: Int
    let createdAt: String?

    var id: String { profileID }

    var scanCount: Int { across * down }

    var dimensionSummary: String { "\(across) × \(down)" }

    init?(fields: [String: JSONValue]) {
        guard
            let profileID = fields["profile_id"]?.stringValue,
            let name = fields["name"]?.stringValue,
            let across = fields["across"]?.intValue,
            let down = fields["down"]?.intValue
        else { return nil }

        self.profileID = profileID
        self.name = name
        self.across = across
        self.down = down
        createdAt = fields["created_at"]?.stringValue
    }
}

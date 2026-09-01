import Foundation

/// One flat-field profile, as `flatfield list` and `flatfield_created`
/// report it — section 3.1's rule again: the library's storage is the CLI's,
/// and Swift only reads back what an event handed it.
///
/// The gain map's path and SHA-256 are deliberately absent: the path is
/// app-private storage the UI has no use for, and the hash is roll-invariant
/// bookkeeping the CLI owns.
struct FlatFieldProfile: Identifiable, Sendable, Hashable {
    let profileID: String
    let name: String
    /// The reference's full-resolution dimensions, for display only; `nil`
    /// when the event did not carry them.
    let referenceWidth: Int?
    let referenceHeight: Int?
    /// Provenance only — the CLI never reads the reference again.
    let sourcePath: String?
    let createdAt: String?

    var id: String { profileID }

    /// Decodes one entry of `flatfield_list`'s `profiles` array or
    /// `flatfield_created`'s `profile` object (`{profile_id, name,
    /// reference_width, reference_height, source_path, created_at}`,
    /// CONTRACT.md). `nil` for a malformed entry — a CLI this version
    /// understands never sends one, but a stream is still read line by line
    /// rather than trusted blindly.
    init?(fields: [String: JSONValue]) {
        guard
            let profileID = fields["profile_id"]?.stringValue,
            let name = fields["name"]?.stringValue
        else { return nil }

        self.profileID = profileID
        self.name = name
        referenceWidth = fields["reference_width"]?.intValue
        referenceHeight = fields["reference_height"]?.intValue
        sourcePath = fields["source_path"]?.stringValue
        createdAt = fields["created_at"]?.stringValue
    }
}
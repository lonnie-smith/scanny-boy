import Foundation

/// One row of `roll list`'s report — section 3.1: the library's whole
/// content, scanned by the CLI, never enumerated by Swift itself.
///
/// An unreadable roll is still one distinct row in the sidebar (section
/// 3.10: "unreadable rolls shown disabled with their reason"), so every
/// field but `path` and `status` is optional.
struct Roll: Identifiable, Sendable, Hashable {
    enum Status: String, Sendable, Hashable {
        case ok
        case unreadable
    }

    struct Reason: Sendable, Hashable {
        let code: CLICode
        let message: String
    }

    let path: URL
    let status: Status
    let reason: Reason?
    let rollID: String?
    let rollName: String?
    /// Excludes superseded negatives (section 3.5).
    let negativeCount: Int?

    /// `rollID` when the roll loaded; the path otherwise, since an
    /// unreadable roll has no `roll_id` to key on but is still one row.
    var id: String { rollID ?? path.path }

    /// The name to show: the roll's own name if it loaded, else the folder
    /// name, so an unreadable roll is still identifiable.
    var displayName: String { rollName ?? path.lastPathComponent }

    init(
        path: URL,
        status: Status,
        reason: Reason?,
        rollID: String?,
        rollName: String?,
        negativeCount: Int?
    ) {
        self.path = path
        self.status = status
        self.reason = reason
        self.rollID = rollID
        self.rollName = rollName
        self.negativeCount = negativeCount
    }

    /// Decodes one entry of `roll_list`'s `rolls` array
    /// (`{path, status, reason, roll_id, roll_name, negative_count}`,
    /// CONTRACT.md). `nil` for a malformed entry — a CLI this version
    /// understands never sends one, but a stream is still read line by
    /// line rather than trusted blindly.
    init?(fields: [String: JSONValue]) {
        guard
            let pathString = fields["path"]?.stringValue,
            let statusString = fields["status"]?.stringValue,
            let status = Status(rawValue: statusString)
        else { return nil }

        var reason: Reason?
        if let reasonFields = fields["reason"]?.objectValue,
            let code = reasonFields["code"]?.stringValue,
            let message = reasonFields["message"]?.stringValue
        {
            reason = Reason(code: CLICode(name: code), message: message)
        }

        self.init(
            path: URL(filePath: pathString),
            status: status,
            reason: reason,
            rollID: fields["roll_id"]?.stringValue,
            rollName: fields["roll_name"]?.stringValue,
            negativeCount: fields["negative_count"]?.intValue
        )
    }
}

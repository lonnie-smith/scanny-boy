import Foundation

/// One row of `roll list`'s report — the library's whole
/// content, scanned by the CLI, never enumerated by Swift itself.
///
/// An unreadable roll is still one distinct row in the sidebar, shown
/// disabled with its reason, so every field but `path` and `status` is
/// optional.
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
    /// The roll's number of negatives — every negative, replacement is
    /// in-place so there is nothing to exclude.
    let negativeCount: Int?
    /// The roll's film stock (the roll-level `film` metadata), when set.
    let film: String?
    /// The roll's setup format, as `FilmFormat`'s raw value (`35mm`,
    /// `6x6`, ...), when set.
    let format: String?

    /// `rollID` when the roll loaded; the path otherwise, since an
    /// unreadable roll has no `roll_id` to key on but is still one row.
    var id: String { rollID ?? path.path }

    /// The name to show: the roll's own name if it loaded, else the folder
    /// name, so an unreadable roll is still identifiable.
    var displayName: String { rollName ?? path.lastPathComponent }

    /// The sidebar's secondary line: the film stock and the negative format,
    /// whichever are set, joined with a middle dot ("Kodak Portra 400 · 6×6").
    /// `nil` when neither is, so the row shows no empty line.
    var filmSummary: String? {
        let parts = [
            film.flatMap { $0.isEmpty ? nil : $0 },
            format.flatMap { FilmFormat(rawValue: $0)?.label },
        ].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    init(
        path: URL,
        status: Status,
        reason: Reason?,
        rollID: String?,
        rollName: String?,
        negativeCount: Int?,
        film: String? = nil,
        format: String? = nil
    ) {
        self.path = path
        self.status = status
        self.reason = reason
        self.rollID = rollID
        self.rollName = rollName
        self.negativeCount = negativeCount
        self.film = film
        self.format = format
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
            negativeCount: fields["negative_count"]?.intValue,
            film: fields["film"]?.stringValue,
            format: fields["format"]?.stringValue
        )
    }
}

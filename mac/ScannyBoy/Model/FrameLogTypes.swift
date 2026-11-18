import Foundation

/// One roll in `framelog rolls`, as the Frame log API passes it through.
struct FrameLogRollSummary: Sendable, Hashable {
    let uid: String
    let rollNumber: Int
    let film: String?
    let camera: String?
    let iso: Int?
    let createdAt: String?
    let finishedAt: String?
    let frameCount: Int
    /// Zero when the API has no last frame.
    let lastFrame: Int
    let firstCapturedAt: String?
    let lastCapturedAt: String?

    init?(fields: [String: JSONValue]) {
        guard
            let uid = fields["uid"]?.stringValue,
            let rollNumber = fields["roll_number"]?.intValue
        else { return nil }

        self.uid = uid
        self.rollNumber = rollNumber
        film = fields["film"]?.stringValue
        camera = fields["camera"]?.stringValue
        iso = fields["iso"]?.intValue
        createdAt = fields["created_at"]?.stringValue
        finishedAt = fields["finished_at"]?.stringValue
        frameCount = fields["frame_count"]?.intValue ?? 0
        lastFrame = fields["last_frame"]?.intValue ?? 0
        firstCapturedAt = fields["first_captured_at"]?.stringValue
        lastCapturedAt = fields["last_captured_at"]?.stringValue
    }
}

/// One frame's display fields in `framelog roll`, in frame order.
struct FrameLogFrame: Sendable, Hashable {
    let uid: String
    let frameNumber: Int
    let capturedLocal: String?
    /// `instant`, `date`, or nil.
    let precision: String?
    let offset: String?
    let lens: String?
    let caption: String?
    let locationName: String?
    let address: String?
    let city: String?
    let state: String?
    let hasPosition: Bool

    init?(fields: [String: JSONValue]) {
        guard
            let uid = fields["uid"]?.stringValue,
            let frameNumber = fields["frame_number"]?.intValue
        else { return nil }

        self.uid = uid
        self.frameNumber = frameNumber
        capturedLocal = fields["captured_local"]?.stringValue
        precision = fields["precision"]?.stringValue
        offset = fields["offset"]?.stringValue
        lens = fields["lens"]?.stringValue
        caption = fields["caption"]?.stringValue
        locationName = fields["location_name"]?.stringValue
        address = fields["address"]?.stringValue
        city = fields["city"]?.stringValue
        state = fields["state"]?.stringValue
        hasPosition = fields["has_position"]?.boolValue ?? false
    }
}

/// One sequenced negative and the frame number it is paired with, or nil
/// when no frame matches.
struct FrameLogAlignmentEntry: Sendable, Hashable {
    let negativeID: String
    let frameNumber: Int?

    init?(fields: [String: JSONValue]) {
        guard let negativeID = fields["negative_id"]?.stringValue else { return nil }

        self.negativeID = negativeID
        frameNumber = fields["frame_number"]?.intValue
    }
}

/// The `framelog_roll` event: one roll with its frames and the default
/// alignment of the local roll's negatives.
struct FrameLogRollDetail: Sendable, Hashable {
    let roll: FrameLogRollSummary
    let missingFrameNumbers: [Int]
    let frames: [FrameLogFrame]
    let alignment: [FrameLogAlignmentEntry]
    /// True when the local roll is already linked to this Frame log roll.
    let linked: Bool

    /// Decodes from the event's own fields (`roll`, `frames`, `alignment`
    /// and `linked`).
    init?(fields: [String: JSONValue]) {
        guard
            let rollFields = fields["roll"]?.objectValue,
            let roll = FrameLogRollSummary(fields: rollFields)
        else { return nil }

        self.roll = roll
        missingFrameNumbers = rollFields["missing_frame_numbers"]?.arrayValue?
            .compactMap(\.intValue) ?? []
        frames = fields["frames"]?.arrayValue?.compactMap { entry in
            entry.objectValue.flatMap { FrameLogFrame(fields: $0) }
        } ?? []
        alignment = fields["alignment"]?.arrayValue?.compactMap { entry in
            entry.objectValue.flatMap { FrameLogAlignmentEntry(fields: $0) }
        } ?? []
        linked = fields["linked"]?.boolValue ?? false
    }
}

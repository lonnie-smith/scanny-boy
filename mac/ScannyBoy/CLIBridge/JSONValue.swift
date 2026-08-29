import Foundation

/// A decoded JSON value of any shape.
///
/// The event stream is the app's machine-readable interface, and the app must
/// survive a CLI that grows a field or an event type it has never heard of.
/// Keeping every line's complete decoded body lets `CLIEvent` expose typed
/// accessors for the fields it knows while still carrying the rest verbatim.
public enum JSONValue: Sendable, Hashable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])
}

extension JSONValue: Decodable {
    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            // Tried before `Double` so integral fields such as `exit_status`
            // and `source_index` stay integers.
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "unrecognised JSON value"
            )
        }
    }
}

extension JSONValue {
    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var intValue: Int? {
        if case .int(let value) = self { return value }
        return nil
    }

    public var arrayValue: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    /// The value as an array of strings, or `nil` if any element is not a
    /// string. Used for `catalogue` and `warnings`.
    public var stringArrayValue: [String]? {
        guard let elements = arrayValue else { return nil }
        var strings: [String] = []
        strings.reserveCapacity(elements.count)
        for element in elements {
            guard let string = element.stringValue else { return nil }
            strings.append(string)
        }
        return strings
    }

    /// The value as an array of string arrays, or `nil` if the shape differs.
    /// Used for `groups`.
    public var nestedStringArrayValue: [[String]]? {
        guard let elements = arrayValue else { return nil }
        var groups: [[String]] = []
        groups.reserveCapacity(elements.count)
        for element in elements {
            guard let strings = element.stringArrayValue else { return nil }
            groups.append(strings)
        }
        return groups
    }
}

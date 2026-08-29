import Foundation

/// Reassembles newline-delimited text from arbitrary byte chunks.
///
/// A pipe read returns whatever bytes happen to be available, so one JSON
/// event line can arrive in several pieces, several lines can arrive in one
/// piece, and a multi-byte UTF-8 character can be split down the middle.
/// Bytes are therefore buffered and only decoded once a whole line is in
/// hand.
struct LineAssembler {
    private var buffer = Data()

    private static let newline = UInt8(ascii: "\n")
    private static let carriageReturn = UInt8(ascii: "\r")

    /// Adds a chunk and returns every line it completed, in order.
    mutating func append(_ chunk: Data) -> [String] {
        buffer.append(chunk)
        var lines: [String] = []
        while let index = buffer.firstIndex(of: Self.newline) {
            let line = buffer[buffer.startIndex..<index]
            lines.append(Self.decode(line))
            buffer = buffer[buffer.index(after: index)...]
        }
        // Re-base so the slice's start index does not grow without bound.
        buffer = Data(buffer)
        return lines
    }

    /// Returns whatever is left after the last newline, once the stream has
    /// ended. A producer that exits without a trailing newline still gets its
    /// final line delivered.
    mutating func flush() -> [String] {
        guard !buffer.isEmpty else { return [] }
        let line = Self.decode(buffer[...])
        buffer = Data()
        return [line]
    }

    /// Decodes one line's bytes, dropping a trailing carriage return.
    ///
    /// Invalid UTF-8 becomes replacement characters rather than throwing: on
    /// stderr that keeps a garbled log line readable, and on stdout it turns
    /// into an ordinary decode failure that names the offending line.
    private static func decode(_ bytes: Data.SubSequence) -> String {
        var bytes = bytes
        if bytes.last == carriageReturn {
            bytes = bytes.dropLast()
        }
        return String(decoding: bytes, as: UTF8.self)
    }
}

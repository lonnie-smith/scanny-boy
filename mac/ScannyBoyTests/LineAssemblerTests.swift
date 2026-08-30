import Foundation
import Testing

@testable import ScannyBoy

@Suite("Line reassembly")
struct LineAssemblerTests {
    @Test("a whole line in one chunk comes straight back")
    func wholeLineInOneChunk() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("hello\n".utf8)) == ["hello"])
    }

    @Test("several lines in one chunk keep their order")
    func severalLinesInOneChunk() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("a\nb\nc\n".utf8)) == ["a", "b", "c"])
    }

    @Test("a JSON object split across reads is reassembled")
    func splitJSONObjectIsReassembled() throws {
        var assembler = LineAssembler()
        #expect(assembler.append(Data(#"{"protocol_version":3,"#.utf8)).isEmpty)
        #expect(assembler.append(Data(#""event":"star"#.utf8)).isEmpty)
        let lines = assembler.append(Data("ted\",\"command\":\"probe\"}\n".utf8))
        #expect(lines.count == 1)

        let event = try CLIEvent(line: try #require(lines.first))
        #expect(event.kind == .started)
        #expect(event.command == "probe")
    }

    @Test("a newline arriving alone completes the pending line")
    func newlineArrivingAloneCompletesTheLine() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("partial".utf8)).isEmpty)
        #expect(assembler.append(Data("\n".utf8)) == ["partial"])
    }

    @Test("one chunk can finish a line and start the next")
    func chunkStraddlesALineBoundary() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("first".utf8)).isEmpty)
        #expect(assembler.append(Data("-half\nsecond".utf8)) == ["first-half"])
        #expect(assembler.append(Data("\n".utf8)) == ["second"])
    }

    @Test("a multi-byte character split across reads is not corrupted")
    func splitMultiByteCharacterSurvives() {
        // "é" is 0xC3 0xA9: the two bytes land in different chunks.
        var assembler = LineAssembler()
        #expect(assembler.append(Data([UInt8(ascii: "c"), 0xC3])).isEmpty)
        #expect(assembler.append(Data([0xA9, UInt8(ascii: "\n")])) == ["cé"])
    }

    @Test("a trailing carriage return is dropped")
    func trailingCarriageReturnIsDropped() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("line\r\n".utf8)) == ["line"])
    }

    @Test("empty lines are preserved as empty strings")
    func emptyLinesArePreserved() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("a\n\nb\n".utf8)) == ["a", "", "b"])
    }

    @Test("a final line without a newline is delivered on flush")
    func flushDeliversTheTrailingLine() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("no trailing newline".utf8)).isEmpty)
        #expect(assembler.flush() == ["no trailing newline"])
    }

    @Test("flush after a complete line yields nothing")
    func flushAfterCompleteLineYieldsNothing() {
        var assembler = LineAssembler()
        #expect(assembler.append(Data("done\n".utf8)) == ["done"])
        #expect(assembler.flush().isEmpty)
    }

    @Test("a line arriving one byte at a time is reassembled")
    func byteAtATimeIsReassembled() {
        var assembler = LineAssembler()
        var lines: [String] = []
        for byte in Data(#"{"protocol_version":3,"event":"finished"}"#.utf8) {
            lines += assembler.append(Data([byte]))
        }
        #expect(lines.isEmpty)
        lines += assembler.append(Data("\n".utf8))
        #expect(lines == [#"{"protocol_version":3,"event":"finished"}"#])
    }
}

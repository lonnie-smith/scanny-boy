import Foundation
import Testing

@testable import ScannyBoy

/// `framelog_rolls` and `framelog_roll` decode from the protocol's
/// snake-case shape (`docs/METADATA_IMPORT_PLAN.md` §5.4).
@Suite("Frame log event decoding")
struct FrameLogEventTests {
    @Test("the framelog event names map both ways")
    func eventNames() {
        #expect(CLIEvent.Kind(name: "framelog_rolls") == .framelogRolls)
        #expect(CLIEvent.Kind(name: "framelog_roll") == .framelogRoll)
        #expect(CLIEvent.Kind.framelogRolls.name == "framelog_rolls")
        #expect(CLIEvent.Kind.framelogRoll.name == "framelog_roll")
    }

    @Test("framelog_rolls decodes each roll and drops a malformed one")
    func rollsDecode() throws {
        let event = try CLIEvent(
            line: TestEvents.line(
                #"""
                {"event":"framelog_rolls","rolls":[
                  {"uid":"k3j9x0a1bc","roll_number":42,"film":"Kodak Portra 400",
                   "camera":"Nikon FM2","iso":400,"created_at":"2026-09-30T14:02:11.000Z",
                   "finished_at":null,"frame_count":6,"last_frame":7,
                   "first_captured_at":"2026-10-01T15:10:00.000Z",
                   "last_captured_at":"2026-10-05T19:58:00.000Z"},
                  {"uid":"no-number"}
                ]}
                """#
            )
        )
        #expect(event.kind == .framelogRolls)
        let rolls = try #require(event.frameLogRolls)
        #expect(rolls.count == 1)
        let roll = try #require(rolls.first)
        #expect(roll.uid == "k3j9x0a1bc")
        #expect(roll.rollNumber == 42)
        #expect(roll.film == "Kodak Portra 400")
        #expect(roll.camera == "Nikon FM2")
        #expect(roll.iso == 400)
        #expect(roll.finishedAt == nil)
        #expect(roll.frameCount == 6)
        #expect(roll.lastFrame == 7)
        #expect(roll.lastCapturedAt == "2026-10-05T19:58:00.000Z")
    }

    @Test("framelog_roll decodes the roll, its frames and the alignment")
    func rollDecodes() throws {
        let event = try CLIEvent(
            line: TestEvents.line(
                #"""
                {"event":"framelog_roll",
                 "roll":{"uid":"k3j9x0a1bc","roll_number":42,"film":"Kodak Portra 400",
                   "camera":"Nikon FM2","iso":400,"created_at":"2026-09-30T14:02:11.000Z",
                   "finished_at":"2026-10-05T20:41:00.000Z","frame_count":3,"last_frame":7,
                   "missing_frame_numbers":[4]},
                 "frames":[
                   {"uid":"a8f2m4q9zz","frame_number":1,"captured_local":"2026-10-01T10:10:00",
                    "precision":"instant","offset":"-05:00","lens":"Nikkor 50mm f/1.8",
                    "caption":"f/8 1/125","location_name":"Holy Name Cathedral",
                    "address":"735 N State St, Chicago, IL 60654","city":"Chicago",
                    "state":"IL","has_position":true},
                   {"uid":"b7k2n5r8tw","frame_number":2,"captured_local":"2026-10-02",
                    "precision":"date","offset":null,"lens":"Nikkor 28mm f/2.8",
                    "caption":null,"location_name":"Millennium Park",
                    "address":null,"city":null,"state":null,"has_position":false},
                   {"uid":"c4d9e1f6gh","frame_number":3,"captured_local":null,
                    "precision":null,"offset":null,"lens":null,"caption":null,
                    "location_name":null,"address":null,"city":null,"state":null,
                    "has_position":false}
                 ],
                 "alignment":[
                   {"negative_id":"n1","frame_number":1},
                   {"negative_id":"n2","frame_number":null}
                 ],
                 "linked":false}
                """#
            )
        )
        #expect(event.kind == .framelogRoll)
        let detail = try #require(event.frameLogRoll)

        #expect(detail.roll.uid == "k3j9x0a1bc")
        #expect(detail.roll.rollNumber == 42)
        #expect(detail.roll.lastFrame == 7)
        #expect(detail.missingFrameNumbers == [4])
        #expect(detail.linked == false)

        #expect(detail.frames.count == 3)
        let first = detail.frames[0]
        #expect(first.frameNumber == 1)
        #expect(first.capturedLocal == "2026-10-01T10:10:00")
        #expect(first.precision == "instant")
        #expect(first.offset == "-05:00")
        #expect(first.city == "Chicago")
        #expect(first.hasPosition)
        let undated = detail.frames[2]
        #expect(undated.capturedLocal == nil)
        #expect(undated.precision == nil)
        #expect(!undated.hasPosition)

        #expect(detail.alignment.count == 2)
        #expect(detail.alignment[0].negativeID == "n1")
        #expect(detail.alignment[0].frameNumber == 1)
        #expect(detail.alignment[1].negativeID == "n2")
        #expect(detail.alignment[1].frameNumber == nil)
    }

    @Test("framelog_roll without a roll object decodes to nothing")
    func rollWithoutRollObjectIsNil() throws {
        let event = try CLIEvent(
            line: TestEvents.line(#"{"event":"framelog_roll","frames":[],"alignment":[]}"#)
        )
        #expect(event.frameLogRoll == nil)
    }
}

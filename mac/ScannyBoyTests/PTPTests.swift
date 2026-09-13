import Foundation
import Testing

@testable import ScannyBoy

@Suite("PTP decoders")
struct PTPTests {
    @Test("decodes a DeviceInfo dataset")
    func deviceInfo() {
        let payload = Self.sampleDeviceInfoPayload()
        let info = PTP.DeviceInfo(payload: Data(payload))
        #expect(info != nil)
        #expect(info?.manufacturer == "Nikon")
        #expect(info?.operations.contains(PTP.getObjectInfo) == true)
        #expect(info?.supportsBufferCapture() == true)
    }

    @Test("decodes an event container")
    func eventContainer() {
        var bytes: [UInt8] = []
        bytes += [16, 0, 0, 0] // length 16
        bytes += [4, 0] // type event
        bytes += [0x02, 0x40] // ObjectAdded
        bytes += [1, 0, 0, 0] // transaction
        bytes += [0x01, 0, 0, 0x0B] // param
        let decoded = PTP.decodeEvent(Data(bytes))
        #expect(decoded?.code == 0x4002)
        #expect(decoded?.params == [0x0B00_0001])
    }

    @Test("decodes CheckEvent queue entries")
    func checkEvent() {
        var payload: [UInt8] = [2, 0] // count
        payload += [0x01, 0xC1, 0x01, 0, 0, 0x0B] // ObjectAddedInSdram + handle
        payload += [0x06, 0x40, 0x0D, 0, 0, 0, 0, 0] // DevicePropChanged
        let events = PTP.decodeCheckEvents(Data(payload))
        #expect(events.count == 2)
        #expect(events[0].code == 0xC101)
        #expect(events[0].param == 0x0B00_0001)
    }

    @Test("decodes ObjectInfo for a buffer frame")
    func objectInfo() {
        let payload = Self.sampleObjectInfoPayload(
            storage: 0, filename: "DSC_0000.NEF", size: 25_000_000
        )
        let info = PTP.ObjectInfo(handle: 0x0B00_0001, payload: Data(payload))
        #expect(info?.filename == "DSC_0000.NEF")
        #expect(info?.storageID == 0)
        #expect(info?.size == 25_000_000)
    }

    @Test("decodes live view headers at each zoom step")
    func liveViewHeaderZoomSteps() {
        let expectedWidths = [6048, 2048, 1024, 512, 256]
        let names = [
            "lv-watch-t00.bin", "lv-watch-t05.bin", "lv-watch-t09.bin",
            "lv-watch-t13.bin", "lv-watch-t17.bin",
        ]
        for (name, expectedWidth) in zip(names, expectedWidths) {
            let payload = LiveViewFixtures.payload(named: name)
            let decoded = PTP.LiveViewHeader.decode(payload)
            #expect(decoded != nil)
            guard let header = decoded?.header else { continue }
            #expect(header.areaSize.width == expectedWidth)
            #expect(header.frameSize.width == 640)
            #expect(header.frameSize.height == 424)
            #expect(header.sensorSize.width == 6048)
        }
    }

    @Test("orderedCommandResult keeps a normal data-then-response pair")
    func orderedCommandResultKeepsNormalPair() {
        let data = PTP.command(PTP.getObjectInfo, params: [0x0B00_0001])
        let response = PTP.response(PTP.responseOK)
        let ordered = PTP.orderedCommandResult(data: data, response: response)
        #expect(PTP.responseCode(ordered.1) == PTP.responseOK)
        #expect(PTP.containerType(ordered.0) == PTP.containerTypeCommand)
    }

    @Test("orderedCommandResult recovers a response left in the first buffer")
    func orderedCommandResultRecoversSwappedResponse() {
        let response = PTP.response(PTP.responseOK)
        let ordered = PTP.orderedCommandResult(data: response, response: Data())
        #expect(ordered.0.isEmpty)
        #expect(PTP.responseCode(ordered.1) == PTP.responseOK)
        #expect(PTP.containerType(ordered.1) == PTP.containerTypeResponse)
    }

    @Test("orderedCommandResult keeps a large data buffer even when bytes 4-5 look like type 3")
    func orderedCommandResultKeepsLargeDataBuffer() {
        let payload = Array(repeating: UInt8(0xAB), count: 1024)
        let dataPhase = Self.dataContainer(payload: payload, tiffLikeHeader: true)
        let ordered = PTP.orderedCommandResult(data: dataPhase, response: Data())
        #expect(ordered.0.count == dataPhase.count)
        #expect(ordered.1.isEmpty)
        #expect(!PTP.isPlausibleResponseContainer(dataPhase))
    }

    @Test("orderedCommandResult swaps a short response ahead of a large data buffer")
    func orderedCommandResultSwapsShortResponseBeforeLargeData() {
        let payload = Array(repeating: UInt8(0xCD), count: 512)
        let dataPhase = Self.dataContainer(payload: payload)
        let response = PTP.response(PTP.responseOK)
        let ordered = PTP.orderedCommandResult(data: response, response: dataPhase)
        #expect(PTP.responseCode(ordered.1) == PTP.responseOK)
        #expect(PTP.payload(ordered.0).count == payload.count)
    }

    @Test("objectPayload picks the buffer whose length matches ObjectInfo")
    func objectPayloadMatchesExpectedSize() {
        let payload = Array(0..<128).map(UInt8.init)
        let dataPhase = Self.dataContainer(payload: payload)
        let response = PTP.response(PTP.responseOK)
        let bytes = PTP.objectPayload(
            dataPhase: dataPhase, response: response, expectedSize: UInt32(payload.count)
        )
        #expect(bytes.count == payload.count)
        #expect(bytes == payload)
    }

    @Test("releaseFailed has a readable description")
    func releaseFailedErrorPresentation() {
        let error: Error = TetherCaptureError.releaseFailed("0x2019 (Device busy)")
        #expect(error.localizedDescription.contains("shutter fired"))
        #expect(error.localizedDescription.contains("0x2019"))
    }

    @Test("rejects truncated and mismatched live view payloads")
    func liveViewHeaderRejections() {
        #expect(PTP.LiveViewHeader.decode(LiveViewFixtures.payload(named: "lv-watch-truncated.bin")) == nil)
        #expect(PTP.LiveViewHeader.decode(LiveViewFixtures.payload(named: "lv-watch-bad-length.bin")) == nil)
    }

    private static func dataContainer(payload: [UInt8], tiffLikeHeader: Bool = false) -> Data {
        var data = Data()
        func append<T: FixedWidthInteger>(_ value: T) {
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        append(UInt32(12 + payload.count))
        if tiffLikeHeader {
            append(UInt16(0x0003))
        } else {
            append(PTP.containerTypeData)
        }
        append(PTP.getObject)
        append(UInt32(0))
        data.append(contentsOf: payload)
        return data
    }

    private static func sampleDeviceInfoPayload() -> [UInt8] {
        var bytes: [UInt8] = []
        func appendU16(_ value: UInt16) {
            bytes.append(UInt8(value & 0xFF))
            bytes.append(UInt8(value >> 8))
        }
        func appendU32(_ value: UInt32) {
            for shift in stride(from: 0, through: 24, by: 8) {
                bytes.append(UInt8((value >> shift) & 0xFF))
            }
        }
        func appendString(_ value: String) {
            let units = Array(value.utf16) + [0]
            bytes.append(UInt8(units.count))
            for unit in units {
                appendU16(unit)
            }
        }
        appendU16(100)
        appendU32(0x0000_000A)
        appendU16(100)
        appendString("Nikon PTP Extensions")
        appendU16(0)
        appendU32(6)
        appendU16(PTP.getObjectInfo)
        appendU16(PTP.getObject)
        appendU16(PTP.getDevicePropValue)
        appendU16(PTP.nikonCheckEvent)
        appendU16(PTP.nikonDeviceReady)
        appendU16(PTP.nikonInitiateCaptureRecInSdram)
        appendU32(1)
        appendU16(0x4002)
        appendU32(1)
        appendU16(0x500D)
        appendU32(0)
        appendU32(0)
        appendString("Nikon")
        appendString("Z f")
        appendString("V2.00")
        return bytes
    }

    private static func sampleObjectInfoPayload(
        storage: UInt32, filename: String, size: UInt32
    ) -> [UInt8] {
        var bytes: [UInt8] = []
        func appendU16(_ value: UInt16) {
            bytes.append(UInt8(value & 0xFF))
            bytes.append(UInt8(value >> 8))
        }
        func appendU32(_ value: UInt32) {
            for shift in stride(from: 0, through: 24, by: 8) {
                bytes.append(UInt8((value >> shift) & 0xFF))
            }
        }
        func appendString(_ value: String) {
            let units = Array(value.utf16) + [0]
            bytes.append(UInt8(units.count))
            for unit in units {
                appendU16(unit)
            }
        }
        appendU32(storage)
        appendU16(0x3801)
        appendU16(0)
        appendU32(size)
        appendU16(0)
        appendU32(0)
        appendU32(0)
        appendU32(0)
        appendU32(6048)
        appendU32(4032)
        appendU32(0)
        appendU32(0)
        appendU16(0)
        appendU32(0)
        appendU32(0)
        appendString(filename)
        appendString("20260911T120000")
        return bytes
    }
}

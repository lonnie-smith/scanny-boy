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

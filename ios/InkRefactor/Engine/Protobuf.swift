import Foundation

public enum GNError: Error, CustomStringConvertible {
    case format(String)
    case unsupported(String)

    public var description: String {
        switch self {
        case .format(let m): return m
        case .unsupported(let m): return m
        }
    }
}

/// Schema-less protobuf: read, write, and surgically patch while preserving unknown fields.
///
/// GoodNotes messages contain fields nobody here has decoded, and some of them are almost
/// certainly load-bearing. Every mutation is therefore field-level surgery on raw bytes:
/// anything not explicitly replaced survives verbatim, which is the only reason a rewritten
/// document is safe to import at all.
public enum PB {
    public static let wireVarint = 0
    public static let wire64 = 1
    public static let wireLen = 2
    public static let wire32 = 5

    /// One top-level field, kept as a range so nothing is copied until it has to be.
    public struct Part {
        public let number: Int
        public let wire: Int
        public let range: Range<Int>          // the whole tag+payload, in the source buffer
        public let payload: Range<Int>        // just the value
    }

    public static func readVarint(_ b: [UInt8], _ i: inout Int) throws -> UInt64 {
        var result: UInt64 = 0
        var shift: UInt64 = 0
        while true {
            guard i < b.count else { throw GNError.format("truncated varint") }
            let byte = b[i]
            i += 1
            result |= UInt64(byte & 0x7F) << shift
            if byte & 0x80 == 0 { return result }
            shift += 7
            guard shift < 64 else { throw GNError.format("varint too long") }
        }
    }

    public static func writeVarint(_ value: UInt64) -> [UInt8] {
        var n = value
        var out: [UInt8] = []
        repeat {
            var byte = UInt8(n & 0x7F)
            n >>= 7
            if n != 0 { byte |= 0x80 }
            out.append(byte)
        } while n != 0
        return out
    }

    public static func tag(_ field: Int, _ wire: Int) -> [UInt8] {
        writeVarint(UInt64(field << 3 | wire))
    }

    public static func bytesField(_ field: Int, _ payload: [UInt8]) -> [UInt8] {
        tag(field, wireLen) + writeVarint(UInt64(payload.count)) + payload
    }

    public static func varintField(_ field: Int, _ value: UInt64) -> [UInt8] {
        tag(field, wireVarint) + writeVarint(value)
    }

    public static func f32Field(_ field: Int, _ value: Float) -> [UInt8] {
        tag(field, wire32) + withUnsafeBytes(of: value.bitPattern.littleEndian) { Array($0) }
    }

    /// -> every top-level field in original order, nothing lost.
    public static func split(_ b: [UInt8]) throws -> [Part] {
        var out: [Part] = []
        var i = 0
        while i < b.count {
            let start = i
            let t = try readVarint(b, &i)
            let field = Int(t >> 3), wire = Int(t & 7)
            let valueStart = i
            switch wire {
            case wireVarint: _ = try readVarint(b, &i)
            case wire64: i += 8
            case wireLen:
                let n = try readVarint(b, &i)
                i += Int(n)
            case wire32: i += 4
            default: throw GNError.format("unsupported wire type \(wire)")
            }
            guard i <= b.count else { throw GNError.format("truncated field \(field)") }
            let payload = wire == wireLen ? (valueStart + varintSize(b, valueStart))..<i
                                          : valueStart..<i
            out.append(Part(number: field, wire: wire, range: start..<i, payload: payload))
        }
        return out
    }

    private static func varintSize(_ b: [UInt8], _ at: Int) -> Int {
        var i = at
        while i < b.count, b[i] & 0x80 != 0 { i += 1 }
        return i - at + 1
    }

    /// Replace or drop fields. `nil` drops. Field order is preserved; untouched bytes
    /// are copied through exactly.
    public static func patch(_ b: [UInt8], _ replacements: [Int: [UInt8]?]) throws -> [UInt8] {
        var out: [UInt8] = []
        out.reserveCapacity(b.count)
        for part in try split(b) {
            if let replacement = replacements[part.number] {
                if let bytes = replacement { out += bytes }
            } else {
                out += b[part.range]
            }
        }
        return out
    }

    /// Replace `field` if present, else insert it keeping ascending field order.
    public static func upsert(_ b: [UInt8], _ field: Int, _ whole: [UInt8]) throws -> [UInt8] {
        let parts = try split(b)
        if parts.contains(where: { $0.number == field }) {
            return try patch(b, [field: whole])
        }
        var out: [UInt8] = []
        var placed = false
        for part in parts {
            if !placed && part.number > field {
                out += whole
                placed = true
            }
            out += b[part.range]
        }
        if !placed { out += whole }
        return out
    }

    /// GoodNotes `.pb` members are streams of varint-length-delimited messages, not single
    /// messages. Reading one as a message is the classic first mistake with this format.
    public static func readStream(_ b: [UInt8]) throws -> [[UInt8]] {
        var out: [[UInt8]] = []
        var i = 0
        while i < b.count {
            let n = Int(try readVarint(b, &i))
            guard i + n <= b.count else { throw GNError.format("truncated stream message") }
            out.append(Array(b[i..<(i + n)]))
            i += n
        }
        return out
    }

    public static func writeStream(_ messages: [[UInt8]]) -> [UInt8] {
        var out: [UInt8] = []
        for m in messages {
            out += writeVarint(UInt64(m.count))
            out += m
        }
        return out
    }

    // Later duplicates win, matching the Python reference. Real records have no duplicate
    // fields, but the two must agree or the implementations drift.
    public static func lastBytes(_ field: Int, in b: [UInt8]) throws -> [UInt8]? {
        var found: [UInt8]?
        for part in try split(b) where part.number == field && part.wire == wireLen {
            found = Array(b[part.payload])
        }
        return found
    }

    public static func lastVarint(_ field: Int, in b: [UInt8]) throws -> UInt64? {
        var found: UInt64?
        for part in try split(b) where part.number == field && part.wire == wireVarint {
            var i = part.payload.lowerBound
            found = try readVarint(b, &i)
        }
        return found
    }

    public static func has(_ field: Int, in b: [UInt8]) throws -> Bool {
        try split(b).contains { $0.number == field }
    }

    /// Colour is a message of `fixed32` floats. Protobuf omits zero-valued fixed32 fields,
    /// so a pure-red stroke has NO field 1 at all. Index by field number and default —
    /// reading positionally silently produces the wrong colour.
    public static func decodeColor(_ b: [UInt8]) throws -> (Double, Double, Double, Double) {
        var c: [Int: Double] = [:]
        for part in try split(b) where part.wire == wire32 {
            let bytes = Array(b[part.payload])
            guard bytes.count == 4 else { continue }
            let bits = UInt32(bytes[0]) | UInt32(bytes[1]) << 8
                     | UInt32(bytes[2]) << 16 | UInt32(bytes[3]) << 24
            c[part.number] = Double(Float(bitPattern: bits))
        }
        return (c[1] ?? 0, c[2] ?? 0, c[3] ?? 0, c[4] ?? 1)
    }

    public static func encodeColor(_ rgba: (Double, Double, Double, Double)) -> [UInt8] {
        let channels = [rgba.0, rgba.1, rgba.2, rgba.3]
        var out: [UInt8] = []
        for (i, v) in channels.enumerated() where v != 0 {
            out += f32Field(i + 1, Float(v))
        }
        return out
    }
}

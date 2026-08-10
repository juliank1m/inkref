import Foundation

/// Reader/writer for `troydhanson/tpl` images, as vendored into GoodNotes.
///
///     "tpl\0" <u32 total length> <NUL-terminated ASCII signature> <members in order>
///
/// The signature is authoritative — never hardcode member offsets. GoodNotes routinely
/// stores float32 bit patterns in `u` slots, so a "uint32" member is very often geometry.
public enum Tpl {
    static let magic: [UInt8] = Array("tpl\0".utf8)

    /// Every scalar is kept as its raw little-endian bit pattern widened to UInt64.
    ///
    /// That looks lossy for the signed types and is not: reading N bytes unsigned and
    /// writing the low N bytes back is bit-exact for two's complement, and it means an
    /// identity re-encode reproduces the source byte for byte without the writer needing
    /// to know which slots were signed.
    public indirect enum Value {
        case scalar(UInt64)
        case list([Value])

        public var scalarValue: UInt64? {
            if case .scalar(let v) = self { return v }
            return nil
        }

        public var items: [Value]? {
            if case .list(let v) = self { return v }
            return nil
        }

        /// The flat unsigned contents of an `A(u)` / `A(v)` member.
        public var flatScalars: [UInt64] {
            guard case .list(let v) = self else { return [] }
            return v.compactMap(\.scalarValue)
        }
    }

    indirect enum Node {
        case scalar(Character)
        case structure([Node])
        case array([Node])
    }

    static func size(of ch: Character) throws -> Int {
        switch ch {
        case "c": return 1
        case "j", "v": return 2
        case "i", "u": return 4
        case "I", "U", "f": return 8
        default: throw GNError.unsupported("unhandled tpl type char \(ch)")
        }
    }

    static func parseSignature(_ sig: String) throws -> [Node] {
        let chars = Array(sig)
        var i = 0
        func inner(_ stop: Character?) throws -> [Node] {
            var out: [Node] = []
            while i < chars.count {
                let c = chars[i]
                if c == stop { i += 1; return out }
                if (c == "A" || c == "S"), i + 1 < chars.count, chars[i + 1] == "(" {
                    i += 2
                    let sub = try inner(")")
                    out.append(c == "A" ? .array(sub) : .structure(sub))
                } else {
                    _ = try size(of: c)
                    out.append(.scalar(c))
                    i += 1
                }
            }
            if stop != nil { throw GNError.format("unbalanced tpl signature \(sig)") }
            return out
        }
        return try inner(nil)
    }

    struct Reader {
        let bytes: [UInt8]
        var offset: Int

        mutating func take(_ n: Int) throws -> ArraySlice<UInt8> {
            guard offset + n <= bytes.count else { throw GNError.format("tpl image truncated") }
            defer { offset += n }
            return bytes[offset..<(offset + n)]
        }

        mutating func scalar(_ ch: Character) throws -> UInt64 {
            let n = try Tpl.size(of: ch)
            var value: UInt64 = 0
            for (k, byte) in (try take(n)).enumerated() {
                value |= UInt64(byte) << (8 * UInt64(k))
            }
            return value
        }

        mutating func count() throws -> Int { Int(try scalar("u")) }
    }

    static func read(_ r: inout Reader, _ nodes: [Node]) throws -> [Value] {
        var out: [Value] = []
        for node in nodes {
            switch node {
            case .scalar(let ch):
                out.append(.scalar(try r.scalar(ch)))
            case .structure(let sub):
                out.append(.list(try read(&r, sub)))
            case .array(let sub):
                let n = try r.count()
                var elements: [Value] = []
                elements.reserveCapacity(n)
                // A(x) of a single scalar yields flat elements; anything else nests.
                if sub.count == 1, case .scalar(let ch) = sub[0] {
                    for _ in 0..<n { elements.append(.scalar(try r.scalar(ch))) }
                } else if sub.count == 1, case .structure(let inner) = sub[0] {
                    for _ in 0..<n { elements.append(.list(try read(&r, inner))) }
                } else {
                    for _ in 0..<n { elements.append(.list(try read(&r, sub))) }
                }
                out.append(.list(elements))
            }
        }
        return out
    }

    static func write(_ nodes: [Node], _ values: [Value], into out: inout [UInt8]) throws {
        guard nodes.count == values.count else {
            throw GNError.format("tpl member count \(values.count) != signature \(nodes.count)")
        }
        func emit(_ v: UInt64, _ n: Int) {
            for k in 0..<n { out.append(UInt8((v >> (8 * UInt64(k))) & 0xFF)) }
        }
        for (node, value) in zip(nodes, values) {
            switch node {
            case .scalar(let ch):
                guard let raw = value.scalarValue else { throw GNError.format("expected scalar") }
                emit(raw, try size(of: ch))
            case .structure(let sub):
                guard let items = value.items else { throw GNError.format("expected struct") }
                try write(sub, items, into: &out)
            case .array(let sub):
                guard let items = value.items else { throw GNError.format("expected array") }
                emit(UInt64(items.count), 4)
                if sub.count == 1, case .scalar(let ch) = sub[0] {
                    let n = try size(of: ch)
                    for e in items {
                        guard let raw = e.scalarValue else { throw GNError.format("expected scalar") }
                        emit(raw, n)
                    }
                } else if sub.count == 1, case .structure(let inner) = sub[0] {
                    for e in items {
                        guard let sv = e.items else { throw GNError.format("expected struct") }
                        try write(inner, sv, into: &out)
                    }
                } else {
                    for e in items {
                        guard let sv = e.items else { throw GNError.format("expected element") }
                        try write(sub, sv, into: &out)
                    }
                }
            }
        }
    }

    public static func load(_ buf: [UInt8]) throws -> (signature: String, members: [Value]) {
        guard buf.count > 8, Array(buf[0..<4]) == magic else {
            throw GNError.format("not a tpl image")
        }
        guard let end = buf[8...].firstIndex(of: 0) else {
            throw GNError.format("tpl signature is not terminated")
        }
        guard let sig = String(bytes: buf[8..<end], encoding: .ascii) else {
            throw GNError.format("tpl signature is not ASCII")
        }
        var reader = Reader(bytes: buf, offset: end + 1)
        return (sig, try read(&reader, try parseSignature(sig)))
    }

    public static func dump(_ signature: String, _ members: [Value]) throws -> [UInt8] {
        var body: [UInt8] = []
        try write(try parseSignature(signature), members, into: &body)
        let sigBytes = Array(signature.utf8)
        let total = UInt32(4 + 4 + sigBytes.count + 1 + body.count)
        var out = magic
        withUnsafeBytes(of: total.littleEndian) { out += Array($0) }
        out += sigBytes
        out.append(0)
        out += body
        return out
    }

    /// GoodNotes packs float32 bit patterns into uint32 slots.
    public static func f32(_ bits: UInt64) -> Double {
        Double(Float(bitPattern: UInt32(truncatingIfNeeded: bits)))
    }

    public static func bits(_ value: Double) -> UInt64 {
        UInt64(Float(value).bitPattern)
    }
}

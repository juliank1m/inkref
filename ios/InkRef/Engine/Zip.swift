import Compression
import Foundation

/// The `.goodnotes` container is an ordinary ZIP, and Foundation has no ZIP reader on iOS,
/// so here is one. Only what the format actually uses: stored and deflate, no Zip64, no
/// encryption.
///
/// Members are kept in their original order with their original compression method and
/// timestamps, because anything untouched must come back out unchanged — GoodNotes accepts
/// a rewritten archive, but only a rewrite that did not quietly reorganise it is safe to
/// trust when nine of ten members were never meant to change.
public struct ZipArchive {
    public struct Entry {
        public var name: String
        public var data: [UInt8]          // always uncompressed
        var method: UInt16                // 0 stored, 8 deflate — preserved on write
        var modTime: UInt16
        var modDate: UInt16
        var externalAttributes: UInt32
        var versionMadeBy: UInt16
    }

    public private(set) var entries: [Entry]

    public init(entries: [Entry]) { self.entries = entries }

    public func index(of name: String) -> Int? {
        entries.firstIndex { $0.name == name }
    }

    public func data(named name: String) -> [UInt8]? {
        index(of: name).map { entries[$0].data }
    }

    /// uuid -> zip path, from index.attachments.pb. Page backgrounds live there.
    public func attachment(_ uuid: String, index: [UInt8]?) -> [UInt8]? {
        guard let index else { return nil }
        for message in (try? PB.readStream(index)) ?? [] {
            guard let idRaw = try? PB.lastBytes(1, in: message),
                  let pathRaw = try? PB.lastBytes(2, in: message),
                  let id = String(bytes: idRaw, encoding: .utf8),
                  let path = String(bytes: pathRaw, encoding: .utf8), id == uuid else { continue }
            return data(named: path)
        }
        return nil
    }

    public mutating func replace(_ name: String, with data: [UInt8]) {
        if let i = index(of: name) { entries[i].data = data }
    }

    // MARK: reading

    public static func read(_ bytes: [UInt8]) throws -> ZipArchive {
        guard let eocd = findEOCD(bytes) else { throw GNError.format("not a ZIP archive") }
        let count = Int(u16(bytes, eocd + 10))
        let cdOffset = Int(u32(bytes, eocd + 16))
        guard u32(bytes, eocd + 16) != 0xFFFF_FFFF, u16(bytes, eocd + 10) != 0xFFFF else {
            throw GNError.unsupported("Zip64 archives are not supported")
        }

        var entries: [Entry] = []
        var p = cdOffset
        for _ in 0..<count {
            guard p + 46 <= bytes.count, u32(bytes, p) == 0x0201_4B50 else {
                throw GNError.format("bad central directory entry")
            }
            let method = u16(bytes, p + 10)
            let compressedSize = Int(u32(bytes, p + 20))
            let uncompressedSize = Int(u32(bytes, p + 24))
            let nameLen = Int(u16(bytes, p + 28))
            let extraLen = Int(u16(bytes, p + 30))
            let commentLen = Int(u16(bytes, p + 32))
            let localOffset = Int(u32(bytes, p + 42))
            guard let name = String(bytes: bytes[(p + 46)..<(p + 46 + nameLen)], encoding: .utf8)
            else { throw GNError.format("member name is not UTF-8") }

            // The local header's sizes can be zeroed when a data descriptor is used; the
            // central directory is the authoritative copy, so read the payload with those.
            guard localOffset + 30 <= bytes.count, u32(bytes, localOffset) == 0x0403_4B50 else {
                throw GNError.format("bad local header for \(name)")
            }
            let lNameLen = Int(u16(bytes, localOffset + 26))
            let lExtraLen = Int(u16(bytes, localOffset + 28))
            let start = localOffset + 30 + lNameLen + lExtraLen
            guard start + compressedSize <= bytes.count else {
                throw GNError.format("truncated member \(name)")
            }
            let payload = Array(bytes[start..<(start + compressedSize)])

            let data: [UInt8]
            switch method {
            case 0: data = payload
            case 8: data = try inflate(payload, expected: uncompressedSize)
            default: throw GNError.unsupported("compression method \(method) in \(name)")
            }

            entries.append(Entry(name: name, data: data, method: method,
                                 modTime: u16(bytes, p + 12), modDate: u16(bytes, p + 14),
                                 externalAttributes: u32(bytes, p + 38),
                                 versionMadeBy: u16(bytes, p + 4)))
            p += 46 + nameLen + extraLen + commentLen
        }
        return ZipArchive(entries: entries)
    }

    // MARK: writing

    public func write() throws -> [UInt8] {
        var out: [UInt8] = []
        var central: [UInt8] = []

        for entry in entries {
            let payload: [UInt8]
            switch entry.method {
            case 0: payload = entry.data
            case 8: payload = try deflate(entry.data)
            default: throw GNError.unsupported("cannot write method \(entry.method)")
            }
            let crc = crc32(entry.data)
            let name = Array(entry.name.utf8)
            let localOffset = out.count

            out += le32(0x0403_4B50)
            out += le16(20)                 // version needed: 2.0, deflate
            out += le16(0)                  // no flags: sizes are in the header, not a trailer
            out += le16(entry.method)
            out += le16(entry.modTime)
            out += le16(entry.modDate)
            out += le32(crc)
            out += le32(UInt32(payload.count))
            out += le32(UInt32(entry.data.count))
            out += le16(UInt16(name.count))
            out += le16(0)
            out += name
            out += payload

            central += le32(0x0201_4B50)
            central += le16(entry.versionMadeBy)
            central += le16(20)
            central += le16(0)
            central += le16(entry.method)
            central += le16(entry.modTime)
            central += le16(entry.modDate)
            central += le32(crc)
            central += le32(UInt32(payload.count))
            central += le32(UInt32(entry.data.count))
            central += le16(UInt16(name.count))
            central += le16(0)              // extra
            central += le16(0)              // comment
            central += le16(0)              // disk number
            central += le16(0)              // internal attributes
            central += le32(entry.externalAttributes)
            central += le32(UInt32(localOffset))
            central += name
        }

        let cdOffset = out.count
        out += central
        out += le32(0x0605_4B50)
        out += le16(0)
        out += le16(0)
        out += le16(UInt16(entries.count))
        out += le16(UInt16(entries.count))
        out += le32(UInt32(central.count))
        out += le32(UInt32(cdOffset))
        out += le16(0)
        return out
    }

    // MARK: bytes

    private static func findEOCD(_ b: [UInt8]) -> Int? {
        guard b.count >= 22 else { return nil }
        let lowest = max(0, b.count - 22 - 0xFFFF)
        var i = b.count - 22
        while i >= lowest {
            if u32(b, i) == 0x0605_4B50 { return i }
            i -= 1
        }
        return nil
    }

    private static func u16(_ b: [UInt8], _ i: Int) -> UInt16 {
        UInt16(b[i]) | UInt16(b[i + 1]) << 8
    }

    private static func u32(_ b: [UInt8], _ i: Int) -> UInt32 {
        UInt32(b[i]) | UInt32(b[i + 1]) << 8 | UInt32(b[i + 2]) << 16 | UInt32(b[i + 3]) << 24
    }
}

private func le16(_ v: UInt16) -> [UInt8] { [UInt8(v & 0xFF), UInt8(v >> 8 & 0xFF)] }

private func le32(_ v: UInt32) -> [UInt8] {
    [UInt8(v & 0xFF), UInt8(v >> 8 & 0xFF), UInt8(v >> 16 & 0xFF), UInt8(v >> 24 & 0xFF)]
}

/// `COMPRESSION_ZLIB` in Apple's library is raw DEFLATE (RFC 1951) with no zlib wrapper,
/// which is precisely what a ZIP member holds.
private func inflate(_ input: [UInt8], expected: Int) throws -> [UInt8] {
    if expected == 0 { return [] }
    var out = [UInt8](repeating: 0, count: expected)
    let written = out.withUnsafeMutableBufferPointer { dst -> Int in
        input.withUnsafeBufferPointer { src in
            compression_decode_buffer(dst.baseAddress!, expected,
                                      src.baseAddress!, input.count, nil, COMPRESSION_ZLIB)
        }
    }
    guard written == expected else { throw GNError.format("inflate produced \(written) of \(expected)") }
    return out
}

private func deflate(_ input: [UInt8]) throws -> [UInt8] {
    if input.isEmpty { return [] }
    let capacity = input.count + input.count / 8 + 4096
    var out = [UInt8](repeating: 0, count: capacity)
    let written = out.withUnsafeMutableBufferPointer { dst -> Int in
        input.withUnsafeBufferPointer { src in
            compression_encode_buffer(dst.baseAddress!, capacity,
                                      src.baseAddress!, input.count, nil, COMPRESSION_ZLIB)
        }
    }
    guard written > 0 else { throw GNError.format("deflate failed") }
    return Array(out[0..<written])
}

private let crcTable: [UInt32] = (0..<256).map { i -> UInt32 in
    var c = UInt32(i)
    for _ in 0..<8 { c = (c & 1) != 0 ? 0xEDB8_8320 ^ (c >> 1) : c >> 1 }
    return c
}

func crc32(_ bytes: [UInt8]) -> UInt32 {
    var c: UInt32 = 0xFFFF_FFFF
    for b in bytes { c = crcTable[Int((c ^ UInt32(b)) & 0xFF)] ^ (c >> 8) }
    return c ^ 0xFFFF_FFFF
}

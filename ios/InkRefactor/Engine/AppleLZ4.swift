import Compression
import Foundation

/// Apple-framed LZ4 — the same `libcompression` codec GoodNotes itself uses.
///
///     bv41 <u32 raw> <u32 comp> <lz4 block>    compressed chunk
///     bv4- <u32 size> <bytes>                  stored chunk
///     bv4$                                     terminator
///
/// `COMPRESSION_LZ4` emits exactly this framing, so there is no encoder to write here.
/// On iOS this is a system framework, which makes it the one part of the format stack that
/// is easier in Swift than in Python.
public enum AppleLZ4 {
    static let compressedMagic: [UInt8] = Array("bv41".utf8)
    static let storedMagic: [UInt8] = Array("bv4-".utf8)
    static let terminator: [UInt8] = Array("bv4$".utf8)

    public static func isFramed(_ b: [UInt8]) -> Bool {
        b.count >= 4 && (Array(b[0..<4]) == compressedMagic || Array(b[0..<4]) == storedMagic)
    }

    private static func u32(_ b: [UInt8], _ at: Int) -> Int {
        Int(UInt32(b[at]) | UInt32(b[at + 1]) << 8
            | UInt32(b[at + 2]) << 16 | UInt32(b[at + 3]) << 24)
    }

    /// Walk the frame headers to get the exact uncompressed size.
    ///
    /// Guessing a buffer size and growing it until the decode fits (what the Python
    /// reference does) works, but a blob whose decode happens to land exactly on the
    /// guess is indistinguishable from one that was truncated. The headers already carry
    /// the answer.
    static func rawSize(_ b: [UInt8]) throws -> Int {
        var total = 0
        var i = 0
        while i + 4 <= b.count {
            let magic = Array(b[i..<(i + 4)])
            if magic == terminator { return total }
            if magic == compressedMagic {
                guard i + 12 <= b.count else { throw GNError.format("truncated bv41 header") }
                total += u32(b, i + 4)
                let comp = u32(b, i + 8)
                i += 12 + comp
            } else if magic == storedMagic {
                guard i + 8 <= b.count else { throw GNError.format("truncated bv4- header") }
                let size = u32(b, i + 4)
                total += size
                i += 8 + size
            } else {
                throw GNError.format("unknown LZ4 frame magic")
            }
        }
        return total
    }

    public static func decompress(_ input: [UInt8]) throws -> [UInt8] {
        guard isFramed(input) else { throw GNError.format("not an Apple LZ4 frame") }
        let capacity = max(try rawSize(input), 1)
        var out = [UInt8](repeating: 0, count: capacity)
        let written = out.withUnsafeMutableBufferPointer { dst -> Int in
            input.withUnsafeBufferPointer { src in
                compression_decode_buffer(dst.baseAddress!, capacity,
                                          src.baseAddress!, input.count,
                                          nil, COMPRESSION_LZ4)
            }
        }
        guard written > 0 else { throw GNError.format("LZ4 decode failed") }
        return Array(out[0..<written])
    }

    public static func compress(_ input: [UInt8]) throws -> [UInt8] {
        // LZ4 can expand incompressible input; the slack covers that plus frame headers.
        let capacity = input.count + input.count / 8 + 4096
        var out = [UInt8](repeating: 0, count: capacity)
        let written = out.withUnsafeMutableBufferPointer { dst -> Int in
            input.withUnsafeBufferPointer { src in
                compression_encode_buffer(dst.baseAddress!, capacity,
                                          src.baseAddress!, input.count,
                                          nil, COMPRESSION_LZ4)
            }
        }
        guard written > 0 else { throw GNError.format("LZ4 encode failed") }
        return Array(out[0..<written])
    }
}

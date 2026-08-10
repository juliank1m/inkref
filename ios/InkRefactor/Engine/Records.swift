import Foundation

/// Descriptor/item record pairs.
///
/// Page content is a stream of strictly alternating (descriptor, item) messages, linked by
/// a shared UUID and a shared {replica, clock} version stamp. All mutation is field-level
/// patching, so fields nobody decoded survive verbatim.
public final class StrokeRecord {
    // item: the top-level field number IS the type
    static let penStroke = 7

    // descriptor fields
    static let dUUID = 1, dVersion = 2, dDeleted = 3, dOrder = 9
    // item body fields
    static let iUUID = 1, iGeometry = 2, iShape = 3, iColor = 4, iDeleted = 14, iVersion = 15

    /// Item body field 3 declares the stroke family and MUST agree with the tpl signature
    /// inside the geometry blob: absent = constant width, 1 = variable width. Disagreement
    /// fails **silently** — the record imports, lands in the live bucket, keeps
    /// byte-correct geometry, and simply never draws. FINDINGS §0b.
    static let shapeForFamily: [String: UInt64?] = [
        StrokeFamily.constantWidth: UInt64?.none,
        StrokeFamily.constantWidthV1: UInt64?.none,
        StrokeFamily.variableWidth: UInt64(1),
    ]

    public private(set) var descriptor: [UInt8]
    public private(set) var item: [UInt8]

    private var bodyCache: [UInt8]?
    private var geometryCache: StrokeGeometry?

    init(descriptor: [UInt8], item: [UInt8]) {
        self.descriptor = descriptor
        self.item = item
    }

    static func isPenStroke(_ item: [UInt8]) -> Bool {
        (try? PB.split(item))?.first?.number == penStroke
    }

    private func body() throws -> [UInt8] {
        if let cached = bodyCache { return cached }
        guard let b = try PB.lastBytes(Self.penStroke, in: item) else {
            throw GNError.format("not a pen-stroke item")
        }
        bodyCache = b
        return b
    }

    private func setBody(_ b: [UInt8]) throws {
        item = try PB.patch(item, [Self.penStroke: PB.bytesField(Self.penStroke, b)])
        bodyCache = b
    }

    public var uuid: String {
        guard let raw = try? PB.lastBytes(Self.iUUID, in: (try? body()) ?? []) else { return "" }
        return String(bytes: raw, encoding: .utf8) ?? ""
    }

    public var order: UInt64? {
        try? PB.lastVarint(Self.dOrder, in: descriptor) ?? nil
    }

    public var color: (Double, Double, Double, Double)? {
        guard let b = try? body(), let raw = try? PB.lastBytes(Self.iColor, in: b) else {
            return nil
        }
        return try? PB.decodeColor(raw)
    }

    /// A tombstone keeps its record but has its geometry arrays emptied, so it decodes as a
    /// stroke whose every member has count 0. Verified across ~69k records: these two
    /// markers are present on 100% of deleted items and 0% of live ones (FINDINGS §0).
    public var isDeleted: Bool {
        let inDescriptor = (try? PB.has(Self.dDeleted, in: descriptor)) ?? false
        let inBody = (try? body()).flatMap { try? PB.has(Self.iDeleted, in: $0) } ?? false
        return inDescriptor || inBody
    }

    public func geometry() throws -> StrokeGeometry {
        if let cached = geometryCache { return cached }
        guard let blob = try PB.lastBytes(Self.iGeometry, in: try body()) else {
            throw GNError.format("stroke has no geometry field")
        }
        let (sig, members) = try Tpl.load(try AppleLZ4.decompress(blob))
        let g = StrokeGeometry(signature: sig, members: members)
        geometryCache = g
        return g
    }

    func setGeometry(_ g: StrokeGeometry) throws {
        guard let shape = Self.shapeForFamily[g.signature] else {
            throw GNError.unsupported(
                "family marker for \(g.signature) is unknown; writing it would risk a "
                + "silently non-rendering stroke")
        }
        let blob = try AppleLZ4.compress(try Tpl.dump(g.signature, g.members))
        var b = try PB.patch(try body(), [Self.iGeometry: PB.bytesField(Self.iGeometry, blob)])
        if let value = shape {
            b = try PB.upsert(b, Self.iShape, PB.varintField(Self.iShape, value))
        } else {
            b = try PB.patch(b, [Self.iShape: [UInt8]?.none])
        }
        try setBody(b)
        geometryCache = g
    }

    /// The only edit this app ever makes to a document.
    public func translate(dx: Double, dy: Double) throws {
        var g = try geometry()
        try g.translate(dx: dx, dy: dy)
        try setGeometry(g)
    }

    /// The two invariants that link a pair: matching UUID, and descriptor f2 byte-identical
    /// to the item body's f15.
    public func isConsistent() -> Bool {
        guard let b = try? body(),
              let du = try? PB.lastBytes(Self.dUUID, in: descriptor),
              let iu = try? PB.lastBytes(Self.iUUID, in: b),
              let dv = try? PB.lastBytes(Self.dVersion, in: descriptor),
              let iv = try? PB.lastBytes(Self.iVersion, in: b) else { return false }
        return du == iu && dv == iv
    }
}

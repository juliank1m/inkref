import Foundation

/// One segment of a stroke's centreline, in whatever space the producer used.
public enum PathSeg: Sendable {
    case move(x: Double, y: Double)
    case quad(cx: Double, cy: Double, x: Double, y: Double)
}

/// Stroke-family dispatch: which tpl members hold coordinates, and how to read and move
/// them. Families are identified by their tpl signature; the signatures were lifted from
/// the shipped GoodNotes binary and match its `PenStrokeShape` type enum 1:1.
///
/// The app only ever reads and translates. It never authors a stroke, which is why a
/// variable-width Apple Pencil page — a family nothing here can write — still beautifies.
public enum StrokeFamily {
    public static let constantWidthV1 = "vuA(v)A(S(uu))A(S(uuuu))"
    public static let constantWidth = "vuA(v)A(S(uu))A(S(uuuu))vA(f)"
    public static let variableWidth = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
    public static let dynamicWidth = "vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"

    /// Where the coordinates live. Members not listed here — widths, angles, opcodes and
    /// the per-segment index arrays — carry no position and must be left exactly alone.
    enum Coords {
        case flat(member: Int, stride: Int)     // x at offset 0, y at offset 1, then skip
        case pairs(member: Int)                 // A(S(uu))   -> (x, y)
        case quads(member: Int)                 // A(S(uuuu)) -> (cx, cy, x, y)
    }

    static let layouts: [String: [Coords]] = [
        constantWidth: [.pairs(member: 3), .quads(member: 4)],
        constantWidthV1: [.pairs(member: 3), .quads(member: 4)],
        // verified on real samples: m2 anchor(x,y,w), m3 operands(x,y,w), m6 (x,y),
        // m8 precomputed render outline (x,y), m9 (x,y,width,angle,angle)
        variableWidth: [.flat(member: 2, stride: 3), .flat(member: 3, stride: 3),
                        .flat(member: 6, stride: 2), .flat(member: 8, stride: 2),
                        .flat(member: 9, stride: 5)],
        dynamicWidth: [.flat(member: 3, stride: 3), .flat(member: 4, stride: 3),
                       .flat(member: 7, stride: 2), .flat(member: 9, stride: 2),
                       .flat(member: 10, stride: 5)],
    ]

    public static func isSupported(_ signature: String) -> Bool {
        layouts[signature] != nil
    }
}

/// A decoded stroke: its tpl members plus everything the app needs to draw and move it.
/// All coordinates here are still in GoodNotes units (1/132 inch); the conversion to
/// points happens exactly once, at the document boundary.
public struct StrokeGeometry {
    public let signature: String
    public var members: [Tpl.Value]

    public init(signature: String, members: [Tpl.Value]) {
        self.signature = signature
        self.members = members
    }

    var isConstant: Bool {
        signature == StrokeFamily.constantWidth || signature == StrokeFamily.constantWidthV1
    }

    var isVariable: Bool {
        signature == StrokeFamily.variableWidth || signature == StrokeFamily.dynamicWidth
    }

    private func member(_ i: Int) -> Tpl.Value? {
        i < members.count ? members[i] : nil
    }

    /// The anchor plus each segment endpoint, in draw order. Control points are excluded:
    /// they are interior to the hull, so bounds taken from these are correct.
    public var onCurvePoints: [(x: Double, y: Double)] {
        if isConstant {
            guard let start = member(3)?.items, let quads = member(4)?.items else { return [] }
            var pts = start.compactMap { pair -> (Double, Double)? in
                guard let v = pair.items, v.count >= 2,
                      let x = v[0].scalarValue, let y = v[1].scalarValue else { return nil }
                return (Tpl.f32(x), Tpl.f32(y))
            }
            for q in quads {
                guard let v = q.items, v.count >= 4,
                      let x = v[2].scalarValue, let y = v[3].scalarValue else { continue }
                pts.append((Tpl.f32(x), Tpl.f32(y)))
            }
            return pts
        }
        if isVariable {
            let offset = signature == StrokeFamily.variableWidth ? 0 : 1
            let anchor = member(2 + offset)?.flatScalars ?? []
            let ops = member(3 + offset)?.flatScalars ?? []
            guard anchor.count >= 3 else { return [] }
            var pts = [(Tpl.f32(anchor[0]), Tpl.f32(anchor[1]))]
            // operands are consumed as (control, end) triplets, six slots per segment
            var i = 0
            while i + 6 <= ops.count {
                pts.append((Tpl.f32(ops[i + 3]), Tpl.f32(ops[i + 4])))
                i += 6
            }
            return pts
        }
        return []
    }

    /// Bounds over on-curve geometry, in units. `nil` when the stroke has no geometry —
    /// which for a tombstone is exactly what happens, since deleting empties the arrays.
    public var bounds: InkBox? {
        let pts = onCurvePoints
        guard !pts.isEmpty else { return nil }
        return InkBox(x0: pts.map(\.x).min()!, y0: pts.map(\.y).min()!,
                      x1: pts.map(\.x).max()!, y1: pts.map(\.y).max()!)
    }

    /// Drawable path plus a nominal width, both in units.
    public var path: (segments: [PathSeg], width: Double) {
        if isConstant {
            let width = member(1)?.scalarValue.map(Tpl.f32) ?? 1
            guard let start = member(3)?.items, let first = start.first?.items,
                  first.count >= 2, let sx = first[0].scalarValue, let sy = first[1].scalarValue
            else { return ([], width) }
            var segs: [PathSeg] = [.move(x: Tpl.f32(sx), y: Tpl.f32(sy))]
            for q in member(4)?.items ?? [] {
                guard let v = q.items, v.count >= 4,
                      let cx = v[0].scalarValue, let cy = v[1].scalarValue,
                      let x = v[2].scalarValue, let y = v[3].scalarValue else { continue }
                segs.append(.quad(cx: Tpl.f32(cx), cy: Tpl.f32(cy),
                                  x: Tpl.f32(x), y: Tpl.f32(y)))
            }
            return (segs, width)
        }
        if isVariable {
            let offset = signature == StrokeFamily.variableWidth ? 0 : 1
            let anchor = member(2 + offset)?.flatScalars ?? []
            let ops = member(3 + offset)?.flatScalars ?? []
            guard anchor.count >= 3, ops.count >= 6 else { return ([], 1) }
            var segs: [PathSeg] = [.move(x: Tpl.f32(anchor[0]), y: Tpl.f32(anchor[1]))]
            var widths = [Tpl.f32(anchor[2])]
            var i = 0
            while i + 6 <= ops.count {
                segs.append(.quad(cx: Tpl.f32(ops[i]), cy: Tpl.f32(ops[i + 1]),
                                  x: Tpl.f32(ops[i + 3]), y: Tpl.f32(ops[i + 4])))
                widths.append(Tpl.f32(ops[i + 5]))
                i += 6
            }
            return (segs, widths.reduce(0, +) / Double(widths.count))
        }
        return ([], 1)
    }

    /// Move every coordinate-bearing member. Widths, angles, opcodes and index members are
    /// untouched, so the shape cannot deform — only its position changes.
    public mutating func translate(dx: Double, dy: Double) throws {
        guard let specs = StrokeFamily.layouts[signature] else {
            throw GNError.unsupported("unsupported stroke family: \(signature)")
        }
        for spec in specs {
            switch spec {
            case .flat(let index, let stride):
                guard index < members.count, var arr = members[index].items else { continue }
                var base = 0
                while base + stride <= arr.count {
                    if let x = arr[base].scalarValue, let y = arr[base + 1].scalarValue {
                        arr[base] = .scalar(Tpl.bits(Tpl.f32(x) + dx))
                        arr[base + 1] = .scalar(Tpl.bits(Tpl.f32(y) + dy))
                    }
                    base += stride
                }
                members[index] = .list(arr)
            case .pairs(let index):
                guard index < members.count, let arr = members[index].items else { continue }
                members[index] = .list(arr.map { element in
                    guard let v = element.items, v.count >= 2,
                          let x = v[0].scalarValue, let y = v[1].scalarValue else { return element }
                    return .list([.scalar(Tpl.bits(Tpl.f32(x) + dx)),
                                  .scalar(Tpl.bits(Tpl.f32(y) + dy))])
                })
            case .quads(let index):
                guard index < members.count, let arr = members[index].items else { continue }
                members[index] = .list(arr.map { element in
                    guard let v = element.items, v.count >= 4,
                          let cx = v[0].scalarValue, let cy = v[1].scalarValue,
                          let x = v[2].scalarValue, let y = v[3].scalarValue else { return element }
                    return .list([.scalar(Tpl.bits(Tpl.f32(cx) + dx)),
                                  .scalar(Tpl.bits(Tpl.f32(cy) + dy)),
                                  .scalar(Tpl.bits(Tpl.f32(x) + dx)),
                                  .scalar(Tpl.bits(Tpl.f32(y) + dy))])
                })
            }
        }
    }
}

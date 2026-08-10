import Foundation

/// Everything in the app speaks **PDF points** (1/72 inch), origin top-left, y down.
/// GoodNotes' 1/132 inch unit is a serialization detail and is converted only inside
/// `GoodNotesDocument`. See FINDINGS §2 — the public parsers get this wrong by 1.8333x.
public struct InkBox: Equatable, Sendable {
    public var x0: Double, y0: Double, x1: Double, y1: Double

    public init(x0: Double, y0: Double, x1: Double, y1: Double) {
        self.x0 = x0; self.y0 = y0; self.x1 = x1; self.y1 = y1
    }

    public var width: Double { x1 - x0 }
    public var height: Double { y1 - y0 }
    public var centerY: Double { (y0 + y1) / 2 }

    public func offset(by o: Offset) -> InkBox {
        InkBox(x0: x0 + o.dx, y0: y0 + o.dy, x1: x1 + o.dx, y1: y1 + o.dy)
    }

    public static func union(_ boxes: [InkBox]) -> InkBox {
        InkBox(x0: boxes.map(\.x0).min() ?? 0, y0: boxes.map(\.y0).min() ?? 0,
               x1: boxes.map(\.x1).max() ?? 0, y1: boxes.map(\.y1).max() ?? 0)
    }
}

/// A pure translation. The only thing the layout engine is ever allowed to emit:
/// nothing scales, rotates or redraws, which is what keeps the output the user's own ink.
public struct Offset: Equatable, Sendable {
    public var dx: Double, dy: Double
    public init(dx: Double = 0, dy: Double = 0) { self.dx = dx; self.dy = dy }
    public var isZero: Bool { dx == 0 && dy == 0 }
    public var magnitude: Double { max(abs(dx), abs(dy)) }
}

/// What a region is. A role never carries a coordinate — it only selects which
/// deterministic rule applies. The AI layer may set it; geometry can guess it.
public enum Role: String, Sendable, CaseIterable {
    case paragraph, heading, bullet, equation, diagram

    /// Ink that is never moved. Aligning a formula or a sketch to a text baseline would
    /// wreck it, and "leave it alone" is always a safe answer.
    public var isFrozen: Bool { self == .equation || self == .diagram }
}

/// Numbers that should go DOWN when a page gets cleaner. All in points.
public struct LayoutMetrics: Equatable, Sendable {
    public var baselineSpread = 0.0
    public var pitchSpread = 0.0
    public var marginSpread = 0.0
    public var gapSpread = 0.0

    public init(baselineSpread: Double = 0, pitchSpread: Double = 0,
                marginSpread: Double = 0, gapSpread: Double = 0) {
        self.baselineSpread = baselineSpread
        self.pitchSpread = pitchSpread
        self.marginSpread = marginSpread
        self.gapSpread = gapSpread
    }

    /// Fractional drop from `before` to self. Positive is better.
    public func improvement(over before: LayoutMetrics) -> LayoutMetrics {
        func d(_ b: Double, _ a: Double) -> Double { b == 0 ? 0 : (b - a) / b }
        return LayoutMetrics(baselineSpread: d(before.baselineSpread, baselineSpread),
                             pitchSpread: d(before.pitchSpread, pitchSpread),
                             marginSpread: d(before.marginSpread, marginSpread),
                             gapSpread: d(before.gapSpread, gapSpread))
    }
}

/// The page as a classifier sees it: geometry only, one record per detected line.
/// This is the entire payload a model is given about structure. It is deliberately not
/// asked where anything should go, and every id it may answer with appears here, so an
/// answer naming anything else is provably invented and gets dropped.
public struct BlockDescription: Codable, Sendable {
    public var id: String
    public var bbox: [Double]          // x0, y0, x1, y1 in points
    public var words: Int
    public var strokes: Int
    public var heightRatio: Double     // line height / page writing height
    public var indentLevel: Int
    public var gapAbove: Double?       // gap to the line above, in pitches
    public var startsWithMark: Bool    // a lone bullet/dash before the text
    public var nearby: [String]

    public init(id: String, bbox: [Double], words: Int, strokes: Int, heightRatio: Double,
                indentLevel: Int, gapAbove: Double?, startsWithMark: Bool, nearby: [String]) {
        self.id = id; self.bbox = bbox; self.words = words; self.strokes = strokes
        self.heightRatio = heightRatio; self.indentLevel = indentLevel
        self.gapAbove = gapAbove; self.startsWithMark = startsWithMark; self.nearby = nearby
    }
}

@inline(__always)
func median(_ values: [Double]) -> Double {
    guard !values.isEmpty else { return 0 }
    let s = values.sorted()
    let m = s.count / 2
    return s.count % 2 == 1 ? s[m] : (s[m - 1] + s[m]) / 2
}

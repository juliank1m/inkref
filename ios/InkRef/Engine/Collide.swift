import Foundation

/// Every stroke on the page, whether or not anything claims it.
///
/// The layout engine reasons about lines and words. The page does not have lines and words
/// on it — it has ink, and 10-15% of it is never recognised: a diagram, a margin note, an
/// arrow, a crossing-out, a symbol the recogniser had no name for. That ink does not move,
/// and it is invisible to a planner that only knows about the ink it grouped. Spacing lines
/// around it drives recognised text straight into it.
///
/// So the plan is checked against *all* the ink before it is applied:
///
///     candidate transformed bounds
///         -> collision against all ink not owned by this moving group
///         -> collision against protected regions (an equation, a diagram)
///         -> page bounds
///         -> safe? apply : reduce, and cancel if it cannot be reduced enough
///
/// The test is **new** overlap, not overlap. Letters within a word already overlap, a
/// descender already reaches into the line below, and a page of dense notes is full of ink
/// that touches. Vetoing any contact would veto everything. What must never happen is ink
/// becoming more entangled than the writer left it.
///
/// Mirrors `inkref/ink/collide.py`.
public enum Collide {
    /// How far a move may be reduced before it is abandoned.
    static let steps: [Double] = [1.0, 0.6, 0.35, 0.0]
    /// Ink may come this much closer than it already was, as a share of the writing height.
    static let slackRatio = 0.02

    public struct Report: Sendable, Equatable {
        public var groups = 0, reduced = 0, cancelled = 0
        public var touched: Int { reduced + cancelled }
    }

    /// A uniform grid over the page: "what else is near here?" without a linear scan.
    /// A page of ten thousand strokes is queried once per moving word, and a scan per query
    /// is ten million box tests — which is how a safety check ends up switched off for
    /// being slow.
    struct InkMap {
        let boxes: [InkBox]
        let cell: Double
        private var grid: [Cell: [Int]] = [:]

        struct Cell: Hashable { let x: Int, y: Int }

        init(_ boxes: [InkBox], refH: Double) {
            self.boxes = boxes
            self.cell = Swift.max(2 * Swift.max(refH, 1e-6), 1e-6)
            for (i, b) in boxes.enumerated() {
                for key in Self.cells(b, cell) { grid[key, default: []].append(i) }
            }
        }

        static func cells(_ b: InkBox, _ cell: Double) -> [Cell] {
            var out: [Cell] = []
            let cx0 = Int((b.x0 / cell).rounded(.down)), cx1 = Int((b.x1 / cell).rounded(.down))
            let cy0 = Int((b.y0 / cell).rounded(.down)), cy1 = Int((b.y1 / cell).rounded(.down))
            guard cx0 <= cx1, cy0 <= cy1 else { return out }
            for x in cx0...cx1 { for y in cy0...cy1 { out.append(Cell(x: x, y: y)) } }
            return out
        }

        func near(_ b: InkBox, pad: Double) -> Set<Int> {
            let probe = InkBox(x0: b.x0 - pad, y0: b.y0 - pad, x1: b.x1 + pad, y1: b.y1 + pad)
            var out = Set<Int>()
            for key in Self.cells(probe, cell) { out.formUnion(grid[key] ?? []) }
            return out
        }
    }

    /// Reduce any part of a plan that would drive ink into other ink.
    ///
    /// What comes back is the planner's proposal with individual groups scaled down or
    /// dropped. Nothing is ever increased and a group that was not moving is never made to
    /// move, so this can only make a plan gentler.
    public static func constrain(_ a: InkLayout.Analysis, boxes: [InkBox],
                                 offsets: [Offset], roles: [Role]? = nil,
                                 page: CGSize? = nil) -> ([Offset], Report) {
        var report = Report()
        guard !a.lines.isEmpty, offsets.contains(where: { !$0.isZero }) else {
            return (offsets, report)
        }
        let ink = InkMap(boxes, refH: a.refH)
        let slack = slackRatio * a.refH
        let role: (Int) -> Role = { k in
            guard let roles, k >= 0, k < roles.count else { return .paragraph }
            return roles[k]
        }

        // Which line each stroke belongs to, and which strokes are protected. Ink inside
        // one line may stay entangled with itself — that is what a line is.
        var lineOf = [Int: Int]()
        var protected = Set<Int>()
        for (k, line) in a.lines.enumerated() {
            let frozen = role(k).isFrozen
            for i in line.indices {
                lineOf[i] = k
                if frozen { protected.insert(i) }
            }
        }

        var out = offsets
        for (k, line) in a.lines.enumerated() {
            for word in line.words {
                let idx = word.indices.filter { $0 < out.count }
                guard !idx.isEmpty, idx.contains(where: { !out[$0].isZero }) else { continue }
                report.groups += 1
                // Every stroke of a word carries the same offset in every current
                // transform; taking the largest keeps this honest if that ever changes.
                let dx = idx.map { out[$0].dx }.max(by: { abs($0) < abs($1) }) ?? 0
                let dy = idx.map { out[$0].dy }.max(by: { abs($0) < abs($1) }) ?? 0

                var best = 0.0
                for scale in steps where scale != 0 {
                    if fits(ink, idx, boxes, out, lineOf, protected, k,
                            dx * scale, dy * scale, slack, page) {
                        best = scale
                        break
                    }
                }
                if best < 1.0 {
                    if best == 0 { report.cancelled += 1 } else { report.reduced += 1 }
                    for i in idx { out[i] = Offset(dx: out[i].dx * best, dy: out[i].dy * best) }
                }
            }
        }
        return (out, report)
    }

    /// True if moving `idx` by (dx, dy) creates no new entanglement and stays on the page.
    private static func fits(_ ink: InkMap, _ idx: [Int], _ boxes: [InkBox],
                             _ offsets: [Offset], _ lineOf: [Int: Int],
                             _ protected: Set<Int>, _ lineK: Int,
                             _ dx: Double, _ dy: Double, _ slack: Double,
                             _ page: CGSize?) -> Bool {
        let own = Set(idx)
        let reach = Swift.max(abs(dx), abs(dy)) + ink.cell
        for i in idx {
            let before = boxes[i]
            let after = before.offset(by: Offset(dx: dx, dy: dy))
            if let page, after.x0 < -slack || after.y0 < -slack
                || after.x1 > page.width + slack || after.y1 > page.height + slack {
                return false
            }
            for j in ink.near(after, pad: reach) {
                if own.contains(j) || j >= offsets.count { continue }
                // Ink inside the same line may keep touching itself.
                if lineOf[j] == lineK, !protected.contains(j) { continue }
                // Judged where the other stroke ENDS UP, not where it started, so the
                // answer does not depend on which group is considered first.
                let otherNow = boxes[j].offset(by: offsets[j])
                let was = overlap(before, boxes[j])
                let now = overlap(after, otherNow)
                if now > was + slack * slack { return false }
                // Protected ink is stricter: an equation or a diagram may not be
                // approached at all, not merely not overlapped further.
                if protected.contains(j), now > 0, was == 0 { return false }
            }
        }
        return true
    }

    static func overlap(_ a: InkBox, _ b: InkBox) -> Double {
        let w = Swift.min(a.x1, b.x1) - Swift.max(a.x0, b.x0)
        let h = Swift.min(a.y1, b.y1) - Swift.max(a.y0, b.y0)
        return w > 0 && h > 0 ? w * h : 0
    }
}

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
    /// Unowned ink this close to a moving group counts as tethered to it, and may not be
    /// left more than `separation` further behind. Both a share of the writing height.
    /// Deliberately looser than the follower rule in `Flow`: a stroke too ambiguous to
    /// travel with a line is exactly the stroke that must not be abandoned by it.
    static let tether = 1.20
    static let separation = 0.50
    /// Passes of the ordering repair. It only ever halves, so it converges: at zero the
    /// layout is the original one, which is ordered by construction.
    static let orderPasses = 4

    public struct Report: Sendable, Equatable {
        public var groups = 0, reduced = 0, cancelled = 0, uncrossed = 0
        public var touched: Int { reduced + cancelled }
    }

    /// stroke index -> owning line. Followers are owned by the line they follow, so a dot
    /// travelling with its line is part of it even though no recognised word claimed it.
    public static func ownership(_ a: InkLayout.Analysis,
                                 followers: [Int: Int] = [:]) -> [Int: Int] {
        var owner = [Int: Int]()
        for (k, line) in a.lines.enumerated() { for i in line.indices { owner[i] = k } }
        for (i, k) in followers where owner[i] == nil { owner[i] = k }
        return owner
    }

    /// Strokes belonging to a region that may not be approached at all.
    public static func protectedStrokes(_ a: InkLayout.Analysis, roles: [Role]?,
                                        followers: [Int: Int] = [:]) -> Set<Int> {
        let role: (Int) -> Role = { k in
            guard let roles, k >= 0, k < roles.count else { return .paragraph }
            return roles[k]
        }
        var out = Set<Int>()
        for (k, line) in a.lines.enumerated() where role(k).isFrozen {
            out.formUnion(line.indices)
        }
        for (i, k) in followers where role(k).isFrozen { out.insert(i) }
        return out
    }

    /// Would moving `indices` (belonging to line `group`) by (dx, dy) be safe?
    /// The predicate `constrain` uses, exposed so a planner can ask before committing.
    public static func fits(_ a: InkLayout.Analysis, boxes: [InkBox], offsets: [Offset],
                            indices: [Int], group: Int, dx: Double, dy: Double,
                            roles: [Role]? = nil, page: CGSize? = nil,
                            followers: [Int: Int] = [:], ink: InkMap? = nil) -> Bool {
        fits(ink ?? InkMap(boxes, refH: a.refH), indices, boxes, offsets,
             ownership(a, followers: followers),
             protectedStrokes(a, roles: roles, followers: followers),
             group, dx, dy, slackRatio * a.refH, page)
    }

    /// How far the line's baseline actually moved.
    ///
    /// Not the median of its strokes' *offsets*. The gate scales word groups
    /// independently, so a line can be left torn — some words moved, some held back — and
    /// the median of the offsets then reports the moved words' shift as the whole line's.
    /// A line that really travelled half as far reads as ordered while it is sitting on top
    /// of the line below it. Fuzzing found 16 such inversions in 5000 runs, the worst two
    /// writing heights deep. `median` averages the two middle values, which matters here:
    /// a line torn exactly in half has no middle element, and taking the upper one hides
    /// the tear all over again.
    static func lineDy(_ line: InkLayout.Line, _ boxes: [InkBox], _ offsets: [Offset]) -> Double {
        let idx = line.indices.filter { $0 < offsets.count && $0 < boxes.count }
        guard !idx.isEmpty else { return 0 }
        return median(idx.map { boxes[$0].y1 + offsets[$0].dy }) - median(idx.map { boxes[$0].y1 })
    }

    /// Undo any reading-order inversion the gate created. Reduces only.
    ///
    /// Word spacing is *cumulative along a line*: each word's shift assumes the ones before
    /// it moved too. The gate judges each word on its own, so holding one back while its
    /// neighbour goes can slide them past each other. Measured once on a real page: one
    /// inversion in about fifteen hundred words. Rare, and unmistakable when it happens.
    ///
    /// Repaired by halving the larger of the two offending offsets. Only ever reducing
    /// keeps the gate's guarantee intact, and at zero the ink sits where the writer left it.
    /// `groups` names what moves as one piece. Without it a repair halves one stroke of a
    /// word and leaves the rest, which un-crosses the pair by tearing the word in two — the
    /// precise thing every other rule here exists to prevent.
    public static func order(_ a: InkLayout.Analysis, _ boxes: [InkBox],
                             _ offsets: [Offset],
                             groups: [([Int], Int)] = []) -> ([Offset], Int) {
        var out = offsets
        var fixed = 0
        var members = [Int: [Int]]()
        for (indices, _) in groups { for i in indices { members[i] = indices } }
        func ease(_ i: Int) {
            for j in members[i] ?? [i] where j < out.count {
                out[j] = Offset(dx: out[j].dx / 2, dy: out[j].dy)
            }
        }
        func easeDy(_ indices: [Int]) {
            for j in indices where j < out.count {
                out[j] = Offset(dx: out[j].dx, dy: out[j].dy / 2)
            }
        }
        for _ in 0..<orderPasses {
            var crossed = 0
            for line in a.lines {
                let words = line.words.filter { !$0.indices.isEmpty }
                for (u, v) in zip(words, words.dropFirst()) {
                    let i = u.indices[0], j = v.indices[0]
                    guard i < out.count, j < out.count else { continue }
                    if boxes[j].x0 + out[j].dx >= boxes[i].x0 + out[i].dx { continue }
                    crossed += 1
                    var k = abs(out[i].dx) >= abs(out[j].dx) ? i : j
                    if out[k].dx == 0 { k = (k == i) ? j : i }
                    ease(k)
                }
            }
            // ...and the same hazard one level up. Baseline alignment gives each word its
            // own dy, the gate reduces them one at a time, and two lines of a column can
            // end up swapped.
            for group in a.blocks {
                let rows = group.sorted { a.lines[$0].baseline < a.lines[$1].baseline }
                for (p, q) in zip(rows, rows.dropFirst()) {
                    let top = a.lines[p].baseline + lineDy(a.lines[p], boxes, out)
                    let bot = a.lines[q].baseline + lineDy(a.lines[q], boxes, out)
                    if bot >= top { continue }
                    crossed += 1
                    let straying = abs(lineDy(a.lines[p], boxes, out))
                        >= abs(lineDy(a.lines[q], boxes, out)) ? p : q
                    let owned = groups.filter { $0.1 == straying }.flatMap(\.0)
                    easeDy(owned.isEmpty ? a.lines[straying].indices : owned)
                }
            }
            fixed += crossed
            if crossed == 0 { break }
        }
        return (out, fixed)
    }

    /// A uniform grid over the page: "what else is near here?" without a linear scan.
    /// A page of ten thousand strokes is queried once per moving word, and a scan per query
    /// is ten million box tests — which is how a safety check ends up switched off for
    /// being slow.
    public struct InkMap {
        let boxes: [InkBox]
        let refH: Double
        let cell: Double
        private var grid: [Cell: [Int]] = [:]

        struct Cell: Hashable { let x: Int, y: Int }

        public init(_ boxes: [InkBox], refH: Double) {
            self.boxes = boxes
            self.refH = Swift.max(refH, 1e-6)
            // Tied to the ink as well as to the writing height: one long stroke on a page
            // whose refH is tiny would otherwise be indexed into millions of cells.
            let span = boxes.filter { $0.x0.isFinite && $0.y1.isFinite }
                .map { Swift.max($0.width, $0.height) }.max() ?? 0
            self.cell = Swift.max(2 * Swift.max(refH, 1e-6), span / 64, 1e-6)
            for (i, b) in boxes.enumerated() {
                for key in Self.cells(b, cell) { grid[key, default: []].append(i) }
            }
        }

        static func cells(_ b: InkBox, _ cell: Double) -> [Cell] {
            var out: [Cell] = []
            // Non-finite geometry would ask for a range from -inf to +inf. It should never
            // get this far, but this is the loop that would hang.
            guard b.x0.isFinite, b.y0.isFinite, b.x1.isFinite, b.y1.isFinite else { return out }
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
                                 page: CGSize? = nil,
                                 groups explicit: [([Int], Int)]? = nil,
                                 followers: [Int: Int] = [:]) -> ([Offset], Report) {
        var report = Report()
        guard !a.lines.isEmpty, offsets.contains(where: { !$0.isZero }) else {
            return (offsets, report)
        }
        let ink = InkMap(boxes, refH: a.refH)
        let slack = slackRatio * a.refH
        let owner = ownership(a, followers: followers)
        let protected = protectedStrokes(a, roles: roles, followers: followers)
        let groups = explicit ?? a.lines.enumerated().flatMap { k, line in
            line.words.map { ($0.indices, k) }
        }

        var out = offsets
        for round in 0..<gateRounds {
            var counted = Report()
            out = gate(a, boxes, out, ink, owner, protected, groups, slack, page, &counted)
            // Only the first pass is reported: the second re-gates what the ordering repair
            // disturbed, and counting those again would double every number.
            if round == 0 { report = counted }
            let (ordered, crossed) = order(a, boxes, out, groups: groups)
            out = ordered
            report.uncrossed += crossed
            if crossed == 0 { break }
        }
        return (out, report)
    }

    /// Gate and ordering repair alternate this many times at most. Both only reduce, so the
    /// sequence is monotone and terminates; two rounds settles every page measured.
    static let gateRounds = 2

    static func gate(_ a: InkLayout.Analysis, _ boxes: [InkBox], _ offsets: [Offset],
                     _ ink: InkMap, _ owner: [Int: Int], _ protected: Set<Int>,
                     _ groups: [([Int], Int)], _ slack: Double, _ page: CGSize?,
                     _ report: inout Report) -> [Offset] {
        var out = offsets
        for (indices, k) in groups {
            let idx = indices.filter { $0 < out.count }
            guard !idx.isEmpty, idx.contains(where: { !out[$0].isZero }) else { continue }
            report.groups += 1
            // Every stroke of a group carries the same offset in every current transform;
            // taking the largest keeps this honest if that ever changes.
            let dx = idx.map { out[$0].dx }.max(by: { abs($0) < abs($1) }) ?? 0
            let dy = idx.map { out[$0].dy }.max(by: { abs($0) < abs($1) }) ?? 0

            var best = 0.0
            for scale in steps where scale != 0 {
                if fits(ink, idx, boxes, out, owner, protected, k,
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
        return out
    }

    /// True if moving `idx` by (dx, dy) creates no new entanglement and stays on the page.
    static func fits(_ ink: InkMap, _ idx: [Int], _ boxes: [InkBox],
                     _ offsets: [Offset], _ owner: [Int: Int],
                     _ protected: Set<Int>, _ lineK: Int,
                     _ dx: Double, _ dy: Double, _ slack: Double,
                     _ page: CGSize?) -> Bool {
        let own = Set(idx)
        let reach = Swift.max(abs(dx), abs(dy)) + ink.cell
        let tetherRange = tether * ink.refH
        let limit = separation * ink.refH
        for i in idx {
            let before = boxes[i]
            let after = before.offset(by: Offset(dx: dx, dy: dy))
            if let page, after.x0 < -slack || after.y0 < -slack
                || after.x1 > page.width + slack || after.y1 > page.height + slack {
                return false
            }
            for j in ink.near(after, pad: reach) {
                if own.contains(j) || j >= offsets.count { continue }
                // Ink owned by the same line may keep touching itself, including the
                // followers that travel with it.
                if owner[j] == lineK, !protected.contains(j) { continue }
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

            // The other danger, and the one collision cannot see: moving AWAY from ink
            // that was part of us. A comma the recogniser missed and the follower rule
            // would not commit to is unowned and stationary; slide its line out from under
            // it and the page tears even though nothing collided.
            for j in ink.near(before, pad: tetherRange) {
                if own.contains(j) || j >= offsets.count || owner[j] != nil { continue }
                if !offsets[j].isZero { continue }   // already moving under another plan
                let nearBefore = gap(before, boxes[j])
                if nearBefore > tetherRange { continue }
                if gap(after, boxes[j]) > Swift.max(nearBefore, 0) + limit { return false }
            }
        }
        return true
    }

    static func gap(_ a: InkBox, _ b: InkBox) -> Double {
        let dx = Swift.max(b.x0 - a.x1, a.x0 - b.x1, 0)
        let dy = Swift.max(b.y0 - a.y1, a.y0 - b.y1, 0)
        return (dx * dx + dy * dy).squareRoot()
    }

    static func overlap(_ a: InkBox, _ b: InkBox) -> Double {
        let w = Swift.min(a.x1, b.x1) - Swift.max(a.x0, b.x0)
        let h = Swift.min(a.y1, b.y1) - Swift.max(a.y0, b.y0)
        return w > 0 && h > 0 ? w * h : 0
    }
}

import Foundation

/// Line spacing: the one correction that moves whole lines past each other.
///
/// It was switched off for a release because it is the only transform that can tear a page.
/// Every other correction moves ink *within* a line, so the worst it does is look odd. This
/// one slides lines through the space between them, and that space is not empty: 10% of a
/// real page is ink no recogniser claimed, and it does not move.
///
/// Four ideas make it safe enough to switch back on, in the order they run:
///
///     followers    unread ink that clearly belongs to one line travels with it
///     blocks       lines are spaced within a flow, never across a column or an equation
///     targets      the desired rhythm comes from the page's own measured pitch
///     acceptance   a whole block's proposed layout is gated before any of it is applied
///
/// The last is the important one. Spacing is not "move each line toward its nearest target"
/// — lines are not independent, and a plan that is safe line by line can still be nonsense
/// as a whole. A block is planned complete, tested complete, and reduced or abandoned
/// complete.
///
/// Nothing here decides a number from semantics. A role says a heading wants more room
/// around it; how much room, and whether there is any, is geometry's answer (SPEC §13).
///
/// Mirrors `inkref/ink/flow.py`.
public enum Flow {
    // Stage 8A. Measured on a real page: of 1082 unclaimed strokes the median is 0.28 of
    // the writing height — dots, commas, accents, fragments of a letter the recogniser
    // split. They are nearly all *small* and *close*. But a tenth of them sit almost
    // equidistant between two lines (second-nearest only 1.11x further), and those are
    // exactly the ones a confident rule would get wrong. Hence a margin test rather than a
    // distance test alone.
    static let followNear = 0.60       // x refH: no further than this from the line's ink
    static let followMargin = 1.60     // ...and the next-nearest must be this much further
    static let followMaxSize = 0.90    // x refH: bigger than this is structure, not a fragment
    static let followSide = 0.50       // x refH: horizontal slop on the line's extent
    // ...and no further than this from the line's BASELINE. The distance above is measured
    // to the line's box, which on mathematical writing is tall — a row carrying an exponent
    // and a subscript spans three writing heights, so ink 0.6 from its box can be 2.8 from
    // the line it would travel with. Measured on a real page before this existed: half the
    // followers sat more than a writing height off the baseline, and the worst was nearly a
    // whole pitch away. An ascender or an accent reaches about 1.2; beyond that it is
    // somebody else's ink.
    static let followBand = 1.60

    // Stage 8B/8C.
    static let blockBreak = 1.70       // x pitch: a wider gap is a deliberate separation
    static let minBlock = 3            // fewer lines has no rhythm worth normalising
    static let headingLead = 1.35      // x pitch: room a heading wants above it
    static let headingTrail = 1.15     // ...and below it
    static let maxBlockShift = 3.0     // x refH: cap on any one line's spacing move

    public struct Report: Sendable, Equatable {
        public var blocks = 0, moved = 0, reduced = 0, dropped = 0
        public var followers = 0, lines = 0
    }

    /// -> [stroke index: line index] for unread ink that clearly belongs to one line.
    ///
    /// A stroke follows AT MOST one line, and only when the answer is not close. Everything
    /// else stays an obstacle, which is the conservative half of the invariant:
    ///
    ///     if unread ink clearly belongs to a moving line, it travels with it;
    ///     if we are not sure, nothing moves it.
    ///
    /// Purely geometric on purpose. The recogniser has already had its say — these are the
    /// strokes it did not claim — so asking it again would be asking the same question of
    /// the same evidence.
    public static func followers(_ a: InkLayout.Analysis, boxes: [InkBox],
                                 unmatched: [Int], roles: [Role]? = nil) -> [Int: Int] {
        guard !a.lines.isEmpty, !unmatched.isEmpty else { return [:] }
        let role: (Int) -> Role = { k in
            guard let roles, k >= 0, k < roles.count else { return .paragraph }
            return roles[k]
        }
        let near = followNear * a.refH
        let side = followSide * a.refH
        let biggest = followMaxSize * a.refH
        let band = followBand * a.refH

        // Only lines that could move are worth following, and a frozen region never adopts:
        // an exponent belonging to an equation must not be handed to the prose beside it.
        let envelopes = a.lines.enumerated().filter { k, line in
            line.isText && !role(k).isFrozen
        }
        guard !envelopes.isEmpty else { return [:] }

        let ink = Collide.InkMap(envelopes.map(\.element.box), refH: a.refH)
        let owned = Collide.ownership(a)
        var out = [Int: Int]()
        for i in unmatched where i < boxes.count {
            let b = boxes[i]
            if Swift.max(b.width, b.height) > biggest { continue }   // structure, not a dot
            var scored: [(Double, Int)] = []
            for slot in ink.near(b, pad: near + side) {
                let (k, line) = envelopes[slot]
                let cx = (b.x0 + b.x1) / 2
                guard cx >= line.box.x0 - side, cx <= line.box.x1 + side else { continue }
                // too far off its baseline to be its ink
                guard abs((b.y0 + b.y1) / 2 - line.baseline) <= band else { continue }
                let d = Collide.gap(b, line.box)
                if d <= near { scored.append((d, k)) }
            }
            guard !scored.isEmpty else { continue }
            scored.sort { $0.0 == $1.0 ? $0.1 < $1.1 : $0.0 < $1.0 }
            let (bestD, bestK) = scored[0]
            // Ambiguous: two lines have an equal claim, so neither gets it.
            if scored.count > 1, scored[1].0 < followMargin * Swift.max(bestD, 1e-6) {
                continue
            }
            if bridges(b, boxes, owned, bestK, a.refH) { continue }
            out[i] = bestK
        }
        return out
    }

    /// -> one group per word, with its followers folded in.
    ///
    /// The gate moves a group as one piece, so a comma only travels with its word if it is
    /// IN that word's group. Left out, the word slides up to a character's width sideways
    /// under the word-spacing correction and its punctuation stays where it was — measured
    /// at 10.8pt on a real page, a comma stranded a whole letter from the word it ends.
    ///
    /// Followers join the word they sit over, by horizontal nearness. The vertical question
    /// was already settled when the follower was assigned to this line.
    public static func wordGroups(_ a: InkLayout.Analysis, _ follow: [Int: Int],
                                  _ boxes: [InkBox]) -> [([Int], Int)] {
        var extra = [Pair: [Int]]()
        for (i, k) in follow where k < a.lines.count && i < boxes.count {
            let words = a.lines[k].words
            guard !words.isEmpty else { continue }
            let cx = (boxes[i].x0 + boxes[i].x1) / 2
            func reach(_ n: Int) -> Double {
                Swift.max(words[n].box.x0 - cx, cx - words[n].box.x1, 0)
            }
            let w = words.indices.min { reach($0) < reach($1) } ?? 0
            extra[Pair(line: k, word: w), default: []].append(i)
        }
        var out: [([Int], Int)] = []
        for (k, line) in a.lines.enumerated() {
            for (n, word) in line.words.enumerated() {
                out.append((word.indices + (extra[Pair(line: k, word: n)] ?? []), k))
            }
        }
        return out
    }

    struct Pair: Hashable { let line: Int, word: Int }

    /// Give every follower the offset already planned for the word it joined.
    ///
    /// Without this the group is torn before the gate ever sees it: the planner has no idea
    /// the follower exists, so it leaves it at zero while its word carries a real shift, and
    /// a gate that only ever scales a group *uniformly* preserves that difference exactly.
    ///
    /// Seeding here rather than after gating is what makes the move honest: the gate then
    /// validates the word and its punctuation as the single object they are.
    public static func adopt(_ groups: [([Int], Int)], _ offsets: [Offset],
                             _ follow: [Int: Int]) -> [Offset] {
        var out = offsets
        for (idx, _) in groups {
            guard let first = idx.first(where: { follow[$0] == nil && $0 < out.count })
            else { continue }
            let o = out[first]
            for i in idx where follow[i] != nil && i < out.count { out[i] = o }
        }
        return out
    }

    /// True if this stroke also reaches ink belonging to a *different* line — a connector,
    /// a bracket or a crossing-out rather than a dot.
    static func bridges(_ box: InkBox, _ boxes: [InkBox], _ owned: [Int: Int],
                        _ lineK: Int, _ refH: Double) -> Bool {
        let pad = 0.1 * refH
        let grown = InkBox(x0: box.x0 - pad, y0: box.y0 - pad,
                           x1: box.x1 + pad, y1: box.y1 + pad)
        for (j, other) in boxes.enumerated() {
            guard let k = owned[j], k != lineK else { continue }
            if Collide.overlap(grown, other) > 0 { return true }
        }
        return false
    }

    /// -> runs of lines that share one vertical flow.
    ///
    /// A block never spans a column, never crosses an equation or a diagram, and never
    /// swallows a deliberate gap. Spacing inside one says nothing about any other, which is
    /// what stops a tight column being "fixed" using a loose column's rhythm.
    public static func blocks(_ a: InkLayout.Analysis, roles: [Role]? = nil) -> [[Int]] {
        let role: (Int) -> Role = { k in
            guard let roles, k >= 0, k < roles.count else { return .paragraph }
            return roles[k]
        }
        var out: [[Int]] = []
        let columns = a.blocks.isEmpty ? [Array(a.lines.indices)] : a.blocks
        for column in columns {
            let rows = column.filter { a.lines[$0].isText }
                .sorted { a.lines[$0].baseline < a.lines[$1].baseline }
            var run: [Int] = []
            for k in rows {
                let frozen = role(k).isFrozen
                let gap = run.isEmpty ? 0 : a.lines[k].baseline - a.lines[run.last!].baseline
                if frozen || (!run.isEmpty && gap > blockBreak * a.pitch) {
                    if run.count >= minBlock { out.append(run) }
                    run = []
                }
                if frozen { continue }   // a protected region is a boundary, never a member
                run.append(k)
            }
            if run.count >= minBlock { out.append(run) }
        }
        return out
    }

    /// -> desired baseline per line of `block`, anchored on its first line.
    ///
    /// The rhythm is the block's own: the median gap between its comparable prose lines,
    /// not a constant and not the page average. A heading asks for more room around it, and
    /// that is the only thing a role contributes — the amount is still measured.
    public static func targets(_ a: InkLayout.Analysis, _ block: [Int],
                               roles: [Role]? = nil) -> [Double] {
        let role: (Int) -> Role = { k in
            guard let roles, k >= 0, k < roles.count else { return .paragraph }
            return roles[k]
        }
        let lines = block.map { a.lines[$0] }
        let gaps = zip(lines, lines.dropFirst()).map { $1.baseline - $0.baseline }
            .filter { $0 > 0 }
        guard !gaps.isEmpty else { return lines.map(\.baseline) }
        // Prose-to-prose gaps only where there are enough of them: a block that is mostly
        // headings should not take its rhythm from the space around them.
        let plain = zip(gaps, zip(block, block.dropFirst())).filter { _, pair in
            role(pair.0) != .heading && role(pair.1) != .heading
        }.map(\.0)
        let pitch = median(plain.count >= 2 ? plain : gaps)

        var out = [lines[0].baseline]
        for (prev, cur) in zip(block, block.dropFirst()) {
            var want = pitch
            if role(prev) == .heading { want = headingTrail * pitch }
            else if role(cur) == .heading { want = headingLead * pitch }
            out.append(out[out.count - 1] + want)
        }
        return out
    }

    /// Stage 8. Adds one shared dy per line, on top of `offsets`.
    ///
    /// The move for a line covers everything that line owns: the strokes its words claimed
    /// *and* the followers assigned to it, all with the same dy. Words are never
    /// repositioned against each other here — the within-line corrections already did that,
    /// and this stage moves the finished line as one piece.
    public static func space(_ a: InkLayout.Analysis, boxes: [InkBox], offsets: [Offset],
                             roles: [Role]? = nil, unmatched: [Int] = [],
                             page: CGSize? = nil,
                             strength s: InkLayout.Strength = .balanced,
                             follow given: [Int: Int]? = nil) -> ([Offset], Report) {
        var report = Report()
        guard !a.lines.isEmpty, a.pitch > 0 else { return (offsets, report) }

        let follow = given ?? followers(a, boxes: boxes, unmatched: unmatched, roles: roles)
        report.followers = follow.count
        var byLine = [Int: [Int]]()
        for (i, k) in follow { byLine[k, default: []].append(i) }

        var out = offsets
        let ink = Collide.InkMap(boxes, refH: a.refH)
        let cap = Swift.min(maxBlockShift * a.refH, s.maxShift * a.refH)

        for block in blocks(a, roles: roles) {
            report.blocks += 1
            let want = targets(a, block, roles: roles)
            // The deadband is the same idea as every other correction: a rhythm already
            // near enough is left alone, and only the excess is taken out.
            let moves = zip(block, want).map { k, target -> Double in
                let err = target - a.lines[k].baseline
                let dy = correct(err, s.line.deadband * a.pitch, s.line.gain)
                return Swift.max(-cap, Swift.min(cap, dy))
            }
            guard moves.contains(where: { abs($0) > 1e-6 }) else { continue }

            let groups = block.map { k in (a.lines[k].indices + (byLine[k] ?? []), k) }
            let (scale, keep) = accept(a, boxes, out, block, groups, moves, roles, page,
                                       ink, follow)
            if keep == 0 { report.dropped += 1; continue }
            if scale < 1.0 || keep < block.count { report.reduced += 1 } else { report.moved += 1 }
            for n in 0..<keep {
                let dy = moves[n] * scale
                if abs(dy) < 1e-9 { continue }
                report.lines += 1
                for i in groups[n].0 where i < out.count {
                    out[i] = Offset(dx: out[i].dx, dy: out[i].dy + dy)
                }
            }
        }
        // Scaling a whole block cannot invert it, but truncating one can at the junction,
        // and collision alone does not notice two lines that swapped without touching.
        return (Collide.order(a, boxes, out, groups: wordGroups(a, follow, boxes)).0, report)
    }

    /// -> (scale, lines kept) — the most of this block's plan that is safe.
    ///
    /// Two knobs, and the order matters. Scaling the *whole* block is tried first because
    /// interpolating between two ordered layouts stays ordered: no amount of uniform easing
    /// can make line 5 cross line 4. Only when no scale works is the block truncated,
    /// keeping a prefix and leaving the rest where it is — safe for the same reason.
    ///
    /// Never the other way round: trimming first would routinely abandon a whole block over
    /// one tight line at the bottom of it.
    static func accept(_ a: InkLayout.Analysis, _ boxes: [InkBox], _ offsets: [Offset],
                       _ block: [Int], _ groups: [([Int], Int)], _ moves: [Double],
                       _ roles: [Role]?, _ page: CGSize?, _ ink: Collide.InkMap,
                       _ follow: [Int: Int]) -> (Double, Int) {
        func safe(_ scale: Double, _ keep: Int) -> Bool {
            var trial = offsets
            for n in 0..<keep {
                let dy = moves[n] * scale
                for i in groups[n].0 where i < trial.count {
                    trial[i] = Offset(dx: trial[i].dx, dy: trial[i].dy + dy)
                }
            }
            for n in 0..<keep {
                let dy = moves[n] * scale
                if abs(dy) < 1e-9 { continue }
                let (idx, k) = groups[n]
                // Tested against the trial offsets, so every other line of this block is
                // judged where it ENDS UP rather than where it started.
                if !Collide.fits(a, boxes: boxes, offsets: trial, indices: idx, group: k,
                                 dx: 0, dy: dy, roles: roles, page: page,
                                 followers: follow, ink: ink) {
                    return false
                }
            }
            return true
        }

        for scale in Collide.steps where scale != 0 {
            if safe(scale, block.count) { return (scale, block.count) }
        }

        // Nothing fits whole. Binary-search the longest prefix that does, at full strength —
        // a shorter move done properly beats a longer one watered down.
        var lo = 0, hi = block.count - 1
        while lo < hi {
            let mid = (lo + hi + 1) / 2
            if safe(1.0, mid) { lo = mid } else { hi = mid - 1 }
        }
        return (1.0, lo)
    }
}

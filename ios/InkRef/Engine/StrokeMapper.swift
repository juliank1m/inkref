import Foundation

/// Recognised boxes -> groups of the user's own strokes.
///
/// The bridge. A recogniser says "there is a word about here"; this decides which original
/// records that sentence is talking about. Nothing here reads the recognised *text* for
/// geometry — the strokes assigned to a group supply every coordinate, so an OCR box that is
/// loose by a few points costs nothing.
///
///     RecognizedLine[]  +  stroke boxes  ->  WordGroup[]  ->  InkLayout.Analysis
///
/// Two rules keep it safe:
///
///   * **A stroke belongs to at most one group.** Otherwise two groups would translate the
///     same ink twice and tear it.
///   * **A stroke that matches nothing is left out.** It then appears in no Word, the
///     planner emits no offset for it, and it stays exactly where the user drew it.
///     Diagrams, doodles and margin scribbles fall out here for free — the recogniser
///     simply does not report them, and that is the correct answer, not a gap.
///
/// Mirrors `inkref/ink/grouping.py`.

/// One recognised word, and the original strokes it is made of.
public struct WordGroup: Sendable {
    public let text: String
    public let indices: [Int]     // indices into the caller's stroke box list
    public let box: InkBox        // union of THOSE strokes, not the recogniser's box
    public let confidence: Double
    public let line: Int          // which recognised line it came from
}

public enum StrokeMapper {
    /// A recognised box is drawn around the letters, not around the ink: a descender, a long
    /// crossbar or the tail of a 'y' routinely sits outside it. Widened by this much of the
    /// line's own height before anything is tested against it.
    static let slopY = 0.40
    static let slopX = 0.60

    /// -> groups in reading order, and the strokes no group claimed.
    ///
    /// Assignment is per stroke rather than per box, which is what makes "at most one
    /// group" true by construction instead of by a later de-duplication pass.
    public static func map(_ lines: [RecognizedLine],
                           boxes: [InkBox]) -> (groups: [WordGroup], unmatched: [Int]) {
        let bands = lines.map { line -> (InkBox, Double, Double) in
            let h = Swift.max(line.box.height, 1e-6)
            return (InkBox(x0: line.box.x0 - slopX * h, y0: line.box.y0 - slopY * h,
                           x1: line.box.x1 + slopX * h, y1: line.box.y1 + slopY * h),
                    line.box.centerY, h)
        }

        var claimed: [Int: (line: Int, word: Int)] = [:]
        var unmatched: [Int] = []
        for (i, s) in boxes.enumerated() {
            let cx = (s.x0 + s.x1) / 2, cy = (s.y0 + s.y1) / 2
            var best: Int? = nil
            var bestScore = Double.infinity
            for (k, band) in bands.enumerated() {
                let (b, mid, h) = band
                guard cx >= b.x0, cx <= b.x1, cy >= b.y0, cy <= b.y1 else { continue }
                // Nearest band centre wins. On tightly spaced writing a stroke can sit
                // inside two bands; the one it is centred in is the one it was written on.
                let score = abs(cy - mid) / h
                if score < bestScore { best = k; bestScore = score }
            }
            if let best {
                claimed[i] = (best, wordOf(s, lines[best].words))
            } else {
                unmatched.append(i)
            }
        }

        var buckets: [Int: [Int: [Int]]] = [:]        // line -> word -> stroke indices
        for (i, at) in claimed { buckets[at.line, default: [:]][at.word, default: []].append(i) }

        var groups: [WordGroup] = []
        for (k, line) in lines.enumerated() {
            for (w, word) in line.words.enumerated() {
                guard let idx = buckets[k]?[w], !idx.isEmpty else { continue }
                groups.append(WordGroup(text: word.text, indices: idx.sorted(),
                                        box: InkBox.union(idx.map { boxes[$0] }),
                                        confidence: word.confidence, line: k))
            }
        }
        return (groups, unmatched.sorted())
    }

    /// Which word of a line a stroke belongs to. Horizontal only — the line already settled
    /// the vertical question, and within one line x is what separates words.
    private static func wordOf(_ stroke: InkBox, _ words: [RecognizedWord]) -> Int {
        var best = 0, bestScore = -1.0
        for (w, word) in words.enumerated() {
            let score = Swift.max(0, Swift.min(stroke.x1, word.box.x1)
                                     - Swift.max(stroke.x0, word.box.x0))
            if score > bestScore { best = w; bestScore = score }
        }
        if bestScore > 0 { return best }
        // No horizontal overlap at all (a stray accent past the end of a word): nearest.
        let cx: Double = (stroke.x0 + stroke.x1) / 2
        func distance(_ w: Int) -> Double {
            let box: InkBox = words[w].box
            return abs((box.x0 + box.x1) / 2 - cx)
        }
        return words.indices.min { distance($0) < distance($1) } ?? 0
    }

    /// Groups -> an `Analysis` the existing planner can use unchanged.
    ///
    /// The structure is the recogniser's; every number in it is measured off the original
    /// strokes. `boxes` stays the caller's full stroke list so the offset array comes back
    /// parallel to it and unmatched strokes get a zero offset rather than disappearing.
    public static func analysis(_ groups: [WordGroup], boxes: [InkBox]) -> InkLayout.Analysis {
        var a = InkLayout.Analysis()
        a.boxCount = boxes.count
        guard !groups.isEmpty else { return a }

        a.refH = refHeight(groups.flatMap(\.indices).map { boxes[$0] })

        var byLine: [Int: [WordGroup]] = [:]
        for g in groups { byLine[g.line, default: []].append(g) }

        // The same correction `analyze` makes, and for the same reason: a per-stroke
        // estimate is badly low on print-style or mathematical writing, where most records
        // are a dot, a bar or an exponent. A recognised line IS a line of writing, so its
        // height is the honest number — and here it comes from a recogniser rather than
        // from clustering.
        let heights = byLine.values.filter { $0.count > 1 }
            .map { InkBox.union($0.map(\.box)).height }.filter { $0 > 0 }
        if !heights.isEmpty {
            a.refH = Swift.max(a.refH, Swift.min(median(heights), 4 * a.refH))
        }

        var lines: [InkLayout.Line] = []
        for key in byLine.keys.sorted() {
            let gs = byLine[key]!.sorted { $0.box.x0 < $1.box.x0 }
            let words = gs.map {
                InkLayout.Word(indices: $0.indices, box: $0.box,
                               baseline: baselineOf($0.indices, boxes, a.refH))
            }
            let idx = gs.flatMap(\.indices)
            lines.append(InkLayout.Line(words: words, box: InkBox.union(gs.map(\.box)),
                                        baseline: baselineOf(idx, boxes, a.refH)))
        }
        a.lines = stableSorted(lines) { $0.baseline }

        let fraction = fractionInk(boxes, a.refH)
        for k in a.lines.indices {
            // Recognised as text, so it is text — that judgement is the recogniser's whole
            // job, and it is a far better one than the width-to-height ratio geometry uses.
            a.lines[k].isText = true
            // Stacked maths still has to be protected. A recogniser reads a fraction as one
            // line and will happily let the planner re-space its numerator away from its
            // denominator, so this check earns its keep even here.
            a.lines[k].rigid = isStacked(a.lines[k], boxes, a.refH)
                || a.lines[k].indices.contains { fraction.contains($0) }
            if a.lines[k].rigid {
                let idx = a.lines[k].indices
                a.lines[k].words = [InkLayout.Word(indices: idx, box: a.lines[k].box,
                                                   baseline: baselineOf(idx, boxes, a.refH))]
            }
        }
        return fuseStacked(InkLayout.statistics(a, boxes), boxes)
    }

    /// A baseline step smaller than this share of the page's own pitch is not a line step.
    /// The distribution on a real page of maths is cleanly bimodal — p25 of the
    /// within-column gaps sat at 8.4pt against a 9.1pt pitch, while the bottom tenth sat at
    /// 5.4pt and below — so this sits in the empty part of it rather than on a slope.
    static let stackPitch = 0.55

    /// Fuse lines that sit too close together to be separate lines.
    ///
    /// `Recognition.mergeStacked` joins readings that *overlap*. It cannot join a numerator
    /// sitting cleanly above its denominator, because from two boxes alone that is
    /// indistinguishable from two lines of prose — and welding prose together would be
    /// worse.
    ///
    /// Once the page has been measured, though, it is distinguishable: two readings a fifth
    /// of a pitch apart, in the same column, are one line of writing with something stacked
    /// in it. Left as two, line spacing pushes them to a full pitch apart, which tears a
    /// fraction in half and drives the halves into the lines above and below — the failure
    /// this exists to stop, seen on a real page before it did.
    ///
    /// The fused line is rigid: it translates whole, and nothing re-spaces inside it.
    static func fuseStacked(_ a: InkLayout.Analysis, _ boxes: [InkBox]) -> InkLayout.Analysis {
        guard !a.lines.isEmpty, a.pitch > 0 else { return a }
        let limit = stackPitch * a.pitch
        // Within a column only. Two columns' lines interleave by baseline, and their
        // spacing says nothing about either.
        var fuse: [Int: Int] = [:]          // line index -> the index it joins
        for group in a.blocks {
            let rows = group.sorted { a.lines[$0].baseline < a.lines[$1].baseline }
            for (prev, cur) in zip(rows, rows.dropFirst())
            where a.lines[cur].baseline - a.lines[prev].baseline < limit {
                fuse[cur] = fuse[prev] ?? prev
            }
        }
        guard !fuse.isEmpty else { return a }

        var merged: [Int: [InkLayout.Line]] = [:]
        var keep: [Int] = []
        for (k, line) in a.lines.enumerated() {
            var root = k
            while let next = fuse[root] { root = next }
            merged[root, default: []].append(line)
            if root == k { keep.append(k) }
        }

        var out = InkLayout.Analysis()
        out.boxCount = a.boxCount
        out.refH = a.refH
        for k in keep {
            let parts = merged[k]!
            if parts.count == 1 { out.lines.append(parts[0]); continue }
            let idx = parts.flatMap { $0.words.flatMap(\.indices) }
            let box = InkBox.union(parts.map(\.box))
            let baseline = baselineOf(idx, boxes, a.refH)
            out.lines.append(InkLayout.Line(
                words: [InkLayout.Word(indices: idx, box: box, baseline: baseline)],
                box: box, baseline: baseline, isText: true, rigid: true))
        }
        out.lines = stableSorted(out.lines) { $0.baseline }
        // Re-measured, not carried over: fusing changes the line count, so pitch, indent
        // levels and the word gap all have to be taken again or they describe the old
        // structure.
        return InkLayout.statistics(out, boxes)
    }

    /// -> (strokes grouped, total). The one number that says whether recognition actually
    /// happened, and the first thing to look at when a page comes back unchanged.
    public static func coverage(_ groups: [WordGroup], _ boxes: [InkBox]) -> (Int, Int) {
        (groups.reduce(0) { $0 + $1.indices.count }, boxes.count)
    }
}

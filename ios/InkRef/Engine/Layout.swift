import Foundation

/// Structure detection and the layout plan. Pure geometry — knows no file format.
///
/// Input is a list of stroke bounding boxes. Output is one `Offset` per stroke.
///
/// Everything this file produces is a **translation**. Nothing here scales, rotates or
/// regenerates a stroke, so whatever the caller applies the offsets to keeps its own shape
/// exactly. That is the product's core promise (SPEC §7) and it is also what makes the
/// GoodNotes side safe: translating a record is the one edit confirmed in the app to leave
/// ink lasso-selectable, erasable and undeformed (FINDINGS, milestone 1).
///
///     boxes -> rows (lines) -> words -> baselines, pitch, indent levels, word gap
///           -> per-word (dx, dy)
///
/// Thresholds are multiples of `refH`, the page's writing height, so the same numbers work
/// at any pen size or page scale.

// Tunables, all multiples of refH unless noted. These match inkref/ink/layout.py exactly;
// a divergence between the two implementations is a bug, not a port decision.
private let rowBaselineTol = 0.45      // a stroke joins a row if its bottom is this close to it
private let rowOverlap = 0.50          // ...or if it vertically overlaps the row this much
private let wordGapMin = 0.55          // a gap must beat this (and 1.8x the median gap) to split words
private let wordGapMedianFactor = 1.8
// ponytail: single-link clustering of line starts. Fine when indents are a clear step apart;
// if drift approaches the indent step the levels merge and lines are pulled to the body
// margin — conservative, but mode-seeking or gap-statistic clustering would do better.
private let indentTol = 0.90           // line starts within this of each other are one indent level
private let minLineGap = 0.35          // of pitch — line spacing may never collapse below this
private let paraRatio = 1.70           // a line gap wider than this x pitch reads as a section break
private let tallRatio = 0.45           // a stroke this tall relative to refH actually sits on the baseline
private let headingLead = 1.35         // of pitch — room opened above a heading
private let headingTrail = 1.15        // ...and below it
private let rowMaxGap = 5.0            // a row is cut where it crosses a gap this wide, x refH
private let columnQuiet = 0.08         // a gutter bin carries at most this share of peak coverage
private let columnGutter = 1.50        // ...and the quiet band must be this wide, x refH
private let columnMinShare = 0.10      // both sides of a cut must hold this share of the strokes
private let markMaxWidth = 0.90        // a first word narrower than this may be a bullet mark

// A row only counts as writing if it is much wider than it is tall. Measured on real ink:
// lines of handwriting run 11-22x, while the strokes of a drawing group into rows of
// 0.7-1.7x. Nothing sits in between, so the threshold is not a fine judgement.
//
// This is the safety net that keeps a sketch, a diagram or a doodle from being aligned to a
// text baseline it never belonged to (SPEC §15: prefer leaving unsupported structures
// unchanged). Pure geometry, so it holds with AI switched off — and a model may freeze
// more, never less.
private let textAspect = 3.0
private let minTextLines = 2          // below this a page is not writing; leave it alone

public enum InkLayout {

    /// A (deadband, gain) pair per transform.
    ///
    /// deadband: error smaller than this is left alone, so natural variation survives.
    /// gain: fraction of the error *beyond* the deadband that is corrected.
    ///
    /// Only the excess is corrected, which keeps the response continuous — no visible jump
    /// for a stroke that happens to sit right on the threshold.
    /// Identity is the name alone: two strengths with the same name are the same setting,
    /// which is what a picker selection has to mean.
    public struct Strength: Sendable, Identifiable, Hashable {
        public var id: String { name }

        public static func == (a: Strength, b: Strength) -> Bool { a.name == b.name }
        public func hash(into hasher: inout Hasher) { hasher.combine(name) }

        public let name: String
        public let baseline: (deadband: Double, gain: Double)   // deadband x refH
        public let line: (deadband: Double, gain: Double)       // deadband x pitch
        public let margin: (deadband: Double, gain: Double)     // deadband x refH
        public let spacing: (deadband: Double, gain: Double)    // deadband x refH
        public let paraRatio: Double    // a line gap wider than this x pitch is a deliberate break
        public let maxShift: Double     // hard cap on |dx| and |dy|, x refH

        public init(name: String,
                    baseline: (deadband: Double, gain: Double),
                    line: (deadband: Double, gain: Double),
                    margin: (deadband: Double, gain: Double),
                    spacing: (deadband: Double, gain: Double),
                    paraRatio: Double, maxShift: Double) {
            self.name = name; self.baseline = baseline; self.line = line
            self.margin = margin; self.spacing = spacing
            self.paraRatio = paraRatio; self.maxShift = maxShift
        }

        // Deadbands are sized against the defects they exist to tolerate, not picked to look
        // cautious: handwriting wobbles off its baseline by roughly 0.1-0.2 of the writing
        // height, drifts off a margin by 0.3-0.6, and varies word gaps by 0.3-0.5. A deadband
        // set above that band does nothing at all, which is how these were first mis-tuned.
        public static let light = Strength(
            name: "light", baseline: (0.18, 0.55), line: (0.28, 0.50),
            margin: (0.50, 0.50), spacing: (0.45, 0.45), paraRatio: 1.8, maxShift: 2.0)
        public static let balanced = Strength(
            name: "balanced", baseline: (0.06, 0.85), line: (0.10, 0.80),
            margin: (0.18, 0.80), spacing: (0.18, 0.75), paraRatio: 1.7, maxShift: 4.0)
        public static let strong = Strength(
            name: "strong", baseline: (0.02, 1.00), line: (0.03, 1.00),
            margin: (0.05, 1.00), spacing: (0.05, 1.00), paraRatio: 1.6, maxShift: 6.0)

        public static let all: [Strength] = [light, balanced, strong]

        public static func named(_ s: String) -> Strength? {
            let key = s.lowercased()
            return all.first { $0.name == key }
        }
    }

    public struct Word: Sendable {
        public let indices: [Int]     // indices into the caller's box list
        public let box: InkBox
        public let baseline: Double   // median stroke bottom

        public init(indices: [Int], box: InkBox, baseline: Double) {
            self.indices = indices; self.box = box; self.baseline = baseline
        }
    }

    public struct Line: Sendable {
        public var words: [Word]
        public var box: InkBox
        public var baseline: Double
        public var level: Int         // index into Analysis.levels, for previews/describe
        public var levelX: Double     // the x this line's indent is measured against
        public var block: Int         // which column this line belongs to
        public var isText: Bool       // false = a drawing row; never moved, never a statistic

        public init(words: [Word], box: InkBox, baseline: Double, level: Int = 0,
                    levelX: Double = 0, block: Int = 0, isText: Bool = true) {
            self.words = words; self.box = box; self.baseline = baseline
            self.level = level; self.levelX = levelX; self.block = block; self.isText = isText
        }

        public var indices: [Int] { words.flatMap(\.indices) }
    }

    public struct Analysis: Sendable {
        public var lines: [Line] = []
        public var refH: Double = 1.0
        public var pitch: Double = 1.0
        public var levels: [Double] = []   // x of each indent level
        public var blocks: [[Int]] = []    // line indices per column, left to right
        public var columns: [Double] = []  // x of each column separator
        public var wordGap: Double = 0.0   // target gap between words
        public var boxCount: Int = 0

        public init() {}

        public var words: [Word] { lines.flatMap(\.words) }
        public var textLines: [Line] { lines.filter(\.isText) }
    }

    // MARK: - structure

    /// `boxes` -> `Analysis`. Empty input gives an empty Analysis.
    public static func analyze(_ boxes: [InkBox]) -> Analysis {
        var a = Analysis()
        a.boxCount = boxes.count
        guard !boxes.isEmpty else { return a }

        // Two passes. A first estimate from individual strokes is biased low, badly so on
        // print-style or mathematical writing where most records are sub-character
        // fragments — a dot, a bar, an exponent — and only a few span the writing height.
        // Real notes measured a stroke median of 1.7pt against a true height near 6pt. A
        // row IS a line of writing, so its height is the honest number.
        a.refH = refHeight(boxes)
        let heights = rowsOf(boxes, a.refH)
            .filter { $0.count > 1 }
            .map { InkBox.union($0.map { boxes[$0] }).height }
        if !heights.isEmpty {
            // Only ever trust it upward; a merged row can overstate, never understate.
            a.refH = Swift.max(a.refH, Swift.min(median(heights), 4.0 * a.refH))
        }

        var lines: [Line] = []
        for row in rowsOf(boxes, a.refH) {
            // A row of unusually small writing gets its own word-split threshold; falling
            // back to the page height would glue its words together.
            let rh = refHeight(row.map { boxes[$0] })
            let rowH = rh == 0 ? a.refH : rh
            let words = wordsOf(row, boxes, rowH).map { w in
                Word(indices: w, box: InkBox.union(w.map { boxes[$0] }),
                     baseline: baselineOf(w, boxes, a.refH))
            }
            lines.append(Line(words: words,
                              box: InkBox.union(row.map { boxes[$0] }),
                              baseline: baselineOf(row, boxes, a.refH)))
        }
        a.lines = stableSorted(lines) { $0.baseline }
        for k in a.lines.indices {
            let box = a.lines[k].box
            a.lines[k].isText = box.height > 0 && box.width >= textAspect * box.height
        }

        // Every statistic below comes from writing only. One tall drawing dropped into a
        // page of notes would otherwise drag the pitch, the margin and the word gap with
        // it, and the text would be aligned to a shape that is not text.
        let text = a.textLines
        a.columns = columnsOf(boxes, a.refH)
        a.blocks = assignColumns(a.lines, a.columns)
            .map { $0.filter { a.lines[$0].isText } }
            .filter { !$0.isEmpty }
        for n in a.blocks.indices {
            a.blocks[n] = stableSorted(a.blocks[n]) { a.lines[$0].baseline }
            for k in a.blocks[n] { a.lines[k].block = n }
        }

        // Pitch and indent levels are per column: a column has its own line rhythm and its
        // own left edge, and mixing two columns' worth of either describes neither.
        var diffs: [Double] = []
        var levels: [Double] = []
        for group in a.blocks {
            let rows = group.map { a.lines[$0] }
            diffs += zip(rows, rows.dropFirst())
                .map { $1.baseline - $0.baseline }.filter { $0 > 0 }
            var local = levelsOf(rows.map { $0.box.x0 }, indentTol * a.refH)
            if local.isEmpty { local = [rows[0].box.x0] }
            for k in group {
                let x = a.lines[k].box.x0
                a.lines[k].levelX = local.min { abs($0 - x) < abs($1 - x) } ?? x
            }
            levels += local
        }
        a.pitch = diffs.isEmpty ? a.refH * 1.6 : median(diffs)

        a.levels = Array(Set(levels)).sorted()
        if a.levels.isEmpty { a.levels = [0.0] }
        for k in a.lines.indices {
            let x = a.lines[k].levelX
            a.lines[k].level = a.levels.indices
                .min { abs(a.levels[$0] - x) < abs(a.levels[$1] - x) } ?? 0
        }

        var gaps: [Double] = []
        for line in text {
            for k in 0..<Swift.max(0, line.words.count - 1) {
                gaps.append(line.words[k + 1].box.x0 - line.words[k].box.x1)
            }
        }
        let positive = gaps.filter { $0 > 0 }
        let target = positive.isEmpty ? 0.6 * a.refH : median(positive)
        a.wordGap = Swift.min(Swift.max(target, 0.40 * a.refH), 1.50 * a.refH)
        return a
    }

    // MARK: - planning

    /// `Analysis` -> offsets parallel to the original box list.
    ///
    /// All translations, composed per word:
    ///   line spacing   (SPEC §8.6)  vertical, whole line
    ///   baseline align (SPEC §8.4)  vertical, per word within its line
    ///   margin align   (SPEC §8.7)  horizontal, whole line, toward its indent level
    ///   word spacing   (SPEC §8.5)  horizontal, cumulative along the line
    ///
    /// `roles` is one role per line, in `a.lines` order — usually from the AI layer, and
    /// `nil` means treat everything as prose. A role never supplies a coordinate; it only
    /// chooses which of the rules above apply, which is the whole point of the split.
    /// `skip` switches individual corrections off by name — "baseline", "line", "margin",
    /// "spacing". A page can be well served by three of them and hurt by the fourth, and
    /// abandoning the whole plan over one throws the other three away.
    public static func plan(_ a: Analysis,
                            strength s: Strength = .balanced,
                            roles: [Role]? = nil,
                            skip: Set<String> = []) -> [Offset] {
        var offsets = [Offset](repeating: Offset(), count: a.boxCount)
        guard !a.lines.isEmpty else { return offsets }
        let role = roleLookup(roles)

        // Too little writing to reason about. A page that is mostly drawing has no baseline
        // grid, no margin and no pitch worth inferring, and guessing one wrecks the page.
        guard a.textLines.count >= minTextLines else { return offsets }

        // Geometry can veto; a role can only add to the veto. A classifier calling a sketch
        // a paragraph must not license moving it.
        let frozen: (Int) -> Bool = { k in
            role(k).isFrozen || !(a.lines.indices.contains(k) && a.lines[k].isText)
        }

        // Line spacing is resolved inside each column; over the page-ordered list a line
        // would be spaced against whichever column happened to sit beside it.
        var targets = a.lines.map(\.baseline)
        for group in (skip.contains("line") ? [] : a.blocks) {
            let local = lineTargets(group.map { a.lines[$0].baseline }, a.pitch, s,
                                    { role(group[$0]) }, { frozen(group[$0]) })
            for (i, k) in group.enumerated() { targets[k] = local[i] }
        }
        let bullets = bulletOffsets(a, role)
        let cap = s.maxShift * a.refH

        for (k, line) in a.lines.enumerated() {
            let r = role(k)
            if frozen(k) { continue }               // keeps Offset(): never touched
            let ldy = targets[k] - line.baseline
            let levelX = line.levelX
            let ldx = skip.contains("margin") ? 0
                : correct(levelX - line.box.x0, s.margin.deadband * a.refH, s.margin.gain)
            let hang = bullets[BulletKey(block: line.block, level: line.level)]
            let listed = r == .bullet && hang != nil && line.words.count >= 2
                && isMark(line.words[0], a.refH)

            var shift = 0.0
            var prevRight: Double? = nil
            for (wi, w) in line.words.enumerated() {
                if let pr = prevRight {
                    if listed, wi == 1, let hang {
                        // hang the item text off a shared offset instead of a generic gap,
                        // so a list reads as a column (SPEC §17.2)
                        let want = levelX + hang
                        shift = correct(want - (w.box.x0 + ldx),
                                        s.spacing.deadband * a.refH, s.spacing.gain)
                    } else {
                        shift += correct(a.wordGap - (w.box.x0 - pr),
                                         s.spacing.deadband * a.refH, s.spacing.gain)
                    }
                }
                prevRight = w.box.x1

                // A word too short to reach the baseline — a hyphen, a dot, an accent — has
                // no baseline of its own to trust, so it rides with its line and nothing else.
                var wdy = 0.0
                if w.box.height >= tallRatio * a.refH {
                    wdy = correct(line.baseline - w.baseline,
                                  s.baseline.deadband * a.refH, s.baseline.gain)
                }

                let dx = clamp(ldx + shift, cap)
                let dy = clamp(ldy + wdy, cap)
                for i in w.indices where offsets.indices.contains(i) {
                    offsets[i] = Offset(dx: dx, dy: dy)
                }
            }
        }
        return offsets
    }

    // MARK: - measuring the result

    /// Numbers that should go DOWN when a page gets cleaner.
    ///
    /// Gaps the engine is *supposed* to leave irregular — a section break, the extra room
    /// around a heading — are excluded. Counting them would score a page as ragged exactly
    /// because its structure was preserved, which is the opposite of the truth.
    public static func metrics(_ boxes: [InkBox],
                               analysis: Analysis? = nil,
                               roles: [Role]? = nil) -> LayoutMetrics {
        let a = analysis ?? analyze(boxes)
        guard !a.lines.isEmpty else { return LayoutMetrics() }
        let role = roleLookup(roles)

        // Only writing is scored. A drawing is never moved, so counting its rows would
        // report a page as ragged because of ink the engine deliberately refused to touch.
        let text = a.lines.enumerated().filter { $0.element.isText }
        var bs: [Double] = [], ps: [Double] = [], ms: [Double] = [], gs: [Double] = []
        for (_, line) in text {
            for i in line.indices where boxes.indices.contains(i) {
                guard boxes[i].height >= tallRatio * a.refH else { continue }
                bs.append(abs(boxes[i].y1 - line.baseline))
            }
            ms.append(abs(line.box.x0 - line.levelX))
            for k in 0..<Swift.max(0, line.words.count - 1) {
                gs.append(abs((line.words[k + 1].box.x0 - line.words[k].box.x1) - a.wordGap))
            }
        }
        for group in a.blocks {
            let rows = group.map { (offset: $0, element: a.lines[$0]) }
            for (top, bottom) in zip(rows, rows.dropFirst()) {
                let d = bottom.element.baseline - top.element.baseline
                guard d <= paraRatio * a.pitch,
                      role(top.offset) != .heading,
                      role(bottom.offset) != .heading else { continue }
                ps.append(abs(d - a.pitch))
            }
        }
        func mean(_ v: [Double]) -> Double { v.isEmpty ? 0 : v.reduce(0, +) / Double(v.count) }
        return LayoutMetrics(baselineSpread: mean(bs), pitchSpread: mean(ps),
                             marginSpread: mean(ms), gapSpread: mean(gs))
    }

    /// The page as a classifier sees it: one record per detected line, geometry only.
    /// Every id a model may answer with appears here, so an answer naming anything else is
    /// provably invented and gets dropped.
    /// A plan that is measured before it is kept.
    ///
    /// Structure detection is a guess, and on a page it reads badly — a dense multi-column
    /// formula sheet, say — a confident plan makes the page worse. Scoring the moved boxes
    /// costs nothing, so a plan that loses is replaced by a gentler one and then by none at
    /// all. Doing nothing is always available and always safe; for a tool that edits
    /// someone's notes, never making a page worse beats squeezing out the last alignment.
    public static func verifiedPlan(_ a: Analysis, boxes: [InkBox],
                                    strength s: Strength = .balanced,
                                    roles: [Role]? = nil)
        -> (offsets: [Offset], used: Strength?, regression: String?) {
        let before = metrics(boxes, analysis: a, roles: roles)
        var hurt: String? = nil
        var skip: Set<String> = []
        for candidate in (s.name == "light" ? [s] : [s, s, .light]) {
            let offsets = plan(a, strength: candidate, roles: roles, skip: skip)
            let shifted = moved(boxes, offsets)
            // scored on the same lines, not a re-analysis: structural churn on a dense
            // page otherwise reads as a regression that no correction caused
            let after = metrics(shifted, analysis: reproject(a, shifted), roles: roles)
            hurt = regressed(before, after, a.refH)
            if hurt == nil { return (offsets, candidate, nil) }
            // Retire only the correction that did the damage and try again. The four are
            // separate promises; a dense formula sheet gains a straight left margin even
            // where its line rhythm is too irregular to normalise.
            if let offender = transformForMetric[hurt ?? ""], !skip.contains(offender) {
                skip.insert(offender)
            } else {
                skip.removeAll()
            }
        }
        return ([Offset](repeating: Offset(), count: a.boxCount), nil, hurt)
    }

    public static func describe(_ a: Analysis) -> [BlockDescription] {
        a.lines.enumerated().map { k, line in
            BlockDescription(
                id: "L\(k)",
                bbox: [line.box.x0, line.box.y0, line.box.x1, line.box.y1].map { round($0, 1) },
                words: line.words.count,
                strokes: line.indices.count,
                heightRatio: a.refH == 0 ? 0 : round(line.box.height / a.refH, 2),
                indentLevel: line.level,
                gapAbove: k == 0 ? nil
                    : round((line.baseline - a.lines[k - 1].baseline) / a.pitch, 2),
                startsWithMark: line.words.count >= 2 && isMark(line.words[0], a.refH),
                looksLikeText: line.isText,
                nearby: [k - 1, k + 1].filter { $0 >= 0 && $0 < a.lines.count }.map { "L\($0)" })
        }
    }

    public static func moved(_ boxes: [InkBox], _ offsets: [Offset]) -> [InkBox] {
        zip(boxes, offsets).map { $0.offset(by: $1) }
    }
}

// MARK: - geometry helpers

/// Estimate the writing height from stroke boxes.
///
/// A plain median is wrong here: an 'A' crossbar, an 'i' dot and a hyphen are separate
/// strokes of nearly zero height, and in a page of print-style handwriting they can
/// outnumber the full-height ones. Take a near-maximum first, then the median of the
/// strokes within reach of it — the letters that actually span the writing height.
private func refHeight(_ boxes: [InkBox]) -> Double {
    let hs = boxes.map(\.height).filter { $0 > 0 }.sorted()
    guard !hs.isEmpty else { return 1.0 }
    let big = hs[Swift.min(hs.count - 1, Int(Double(hs.count) * 0.9))]   // robust near-max
    let tall = hs.filter { $0 >= 0.35 * big }
    return tall.isEmpty ? big : median(tall)
}

/// Where a group of strokes sits. Bars and dots are excluded for the same reason.
private func baselineOf(_ idxs: [Int], _ boxes: [InkBox], _ refH: Double) -> Double {
    let bottoms = idxs.filter { boxes[$0].height >= tallRatio * refH }.map { boxes[$0].y1 }
    return median(bottoms.isEmpty ? idxs.map { boxes[$0].y1 } : bottoms)
}

/// Group stroke indices into text rows.
///
/// Two passes, and the order matters. Full-height strokes seed the rows first, sorted by
/// bottom edge — a baseline is what a row actually shares, and a bottom edge barely moves
/// where a y-centre swings with ascenders. Only then are the short strokes attached: a
/// crossbar, a dot, a hyphen, the top bar of a `T`. Those carry no baseline of their own,
/// and seeding a row with one puts a row 22 pt above the text it belongs to, which then
/// refuses every real letter that arrives after it.
private func rowsOf(_ boxes: [InkBox], _ refH: Double) -> [[Int]] {
    var tall = boxes.indices.filter { boxes[$0].height >= tallRatio * refH }
    if tall.isEmpty { tall = Array(boxes.indices) }   // a page of dots; nothing better to do
    let tallSet = Set(tall)
    let short = boxes.indices.filter { !tallSet.contains($0) }

    // Each row carries running aggregates instead of being re-measured. Recomputing a
    // row's baseline and extent for every candidate makes matching
    // O(strokes x rows x row size) — about 115M operations on a 10,000-stroke page. The
    // bottoms are kept sorted so the median is an index rather than a sort, which makes
    // the result identical to the naive version, not an approximation of it.
    struct Row {
        var indices: [Int]
        var y0: Double
        var y1: Double
        var tallBottoms: [Double]
        var allBottoms: [Double]

        init(_ i: Int, _ b: InkBox, tall: Bool) {
            indices = [i]; y0 = b.y0; y1 = b.y1
            allBottoms = [b.y1]
            tallBottoms = tall ? [b.y1] : []
        }

        mutating func add(_ i: Int, _ b: InkBox, tall: Bool) {
            indices.append(i)
            y0 = Swift.min(y0, b.y0)
            y1 = Swift.max(y1, b.y1)
            Row.insort(&allBottoms, b.y1)
            if tall { Row.insort(&tallBottoms, b.y1) }
        }

        static func insort(_ a: inout [Double], _ v: Double) {
            var lo = 0, hi = a.count
            while lo < hi {
                let mid = (lo + hi) / 2
                if a[mid] < v { lo = mid + 1 } else { hi = mid }
            }
            a.insert(v, at: lo)
        }

        var baseline: Double {
            let v = tallBottoms.isEmpty ? allBottoms : tallBottoms
            let m = v.count / 2
            return v.count % 2 == 1 ? v[m] : (v[m - 1] + v[m]) / 2
        }
    }

    var rows: [Row] = []
    for i in stableSorted(tall, by: { boxes[$0].y1 }) {
        let b = boxes[i]
        var best: Int? = nil
        var bestErr = Double.infinity
        for (r, row) in rows.enumerated() {
            let rb = row.baseline
            let overlap = Swift.min(b.y1, row.y1) - Swift.max(b.y0, row.y0)
            let h = Swift.min(b.height, row.y1 - row.y0)
            let err = abs(b.y1 - rb)
            let fits = err <= rowBaselineTol * refH || (h > 0 && overlap / h >= rowOverlap)
            if fits && err < bestErr { best = r; bestErr = err }
        }
        if let best { rows[best].add(i, b, tall: true) } else { rows.append(Row(i, b, tall: true)) }
    }

    for i in stableSorted(short, by: { boxes[$0].y1 }) {
        let b = boxes[i]
        var best: Int? = nil
        var bestScore = 0.0
        for (r, row) in rows.enumerated() {
            let score = Swift.min(b.y1, row.y1) - Swift.max(b.y0, row.y0)
            if score > bestScore { best = r; bestScore = score }
        }
        if best == nil {                                 // not inside any row: nearest one
            var nearest: Int? = nil
            var nearestD = Double.infinity
            for (r, row) in rows.enumerated() {
                let d = abs(b.centerY - row.baseline)
                if d <= 1.5 * refH && d < nearestD { nearest = r; nearestD = d }
            }
            best = nearest
        }
        if let best { rows[best].add(i, b, tall: false) } else { rows.append(Row(i, b, tall: false)) }
    }

    let grouped = rows.map(\.indices)

    // Only now split a row where it crosses a wide horizontal gap. Doing it while building
    // rows makes the result depend on the order strokes arrive in — a stroke at the far
    // right is compared against a row holding only its left half, and is thrown into a row
    // of its own. Splitting a finished row cannot go wrong that way.
    let maxGap = rowMaxGap * refH
    var out: [[Int]] = []
    for row in grouped {
        let ordered = stableSorted(row) { boxes[$0].x0 }
        guard let first = ordered.first else { continue }
        var piece = [first]
        var reach = boxes[first].x1
        for i in ordered.dropFirst() {
            if boxes[i].x0 - reach > maxGap {
                out.append(piece)
                piece = []
            }
            piece.append(i)
            reach = Swift.max(reach, boxes[i].x1)
        }
        out.append(piece)
    }
    return out
}

/// -> x positions separating the page's columns, left to right. Empty means one column.
///
/// Every vertical rule — pitch, section breaks, ordering — is meaningless across two
/// columns that merely sit at the same height. A four-column page sorted by baseline
/// interleaves all four and the measured "line spacing" becomes the distance between
/// neighbouring columns: 0.4pt on real notes whose lines are 8pt apart.
///
/// Found by vertical projection. Chaining lines by x-overlap does not work — one wide line
/// spanning two columns links them, and transitively the page collapses into one block.
/// A gutter is a band quiet down the *entire* page, which no single line can forge. Fully
/// empty gutters are rare, so the test is near-quiet, and a cut is taken only when both
/// sides hold a real share of the ink.
private func columnsOf(_ boxes: [InkBox], _ refH: Double) -> [Double] {
    guard Double(boxes.count) >= 4.0 / columnMinShare else { return [] }
    let x0 = boxes.map(\.x0).min() ?? 0
    let x1 = boxes.map(\.x1).max() ?? 0
    let span = x1 - x0
    let gutterMin = Swift.max(columnGutter * refH, 1e-9)
    guard span > 4 * gutterMin else { return [] }

    let bins = Swift.max(16, Int(span / Swift.max(refH * 0.5, 1e-6)))
    let width = span / Double(bins)
    var cover = [Int](repeating: 0, count: bins)
    for b in boxes {
        let lo = Swift.min(bins - 1, Swift.max(0, Int((b.x0 - x0) / width)))
        let hi = Swift.min(bins - 1, Swift.max(0, Int((b.x1 - x0) / width)))
        if lo <= hi { for i in lo...hi { cover[i] += 1 } }
    }
    guard let peak = cover.max(), peak > 0 else { return [] }

    let quiet = Double(peak) * columnQuiet
    var runs: [(Int, Int)] = []
    var start: Int? = nil
    for (i, c) in cover.enumerated() {
        if Double(c) <= quiet, start == nil { start = i }
        else if Double(c) > quiet, let s = start { runs.append((s, i)); start = nil }
    }
    if let s = start { runs.append((s, bins)) }

    var cuts: [Double] = []
    for (lo, hi) in runs {
        if lo == 0 || hi == bins { continue }            // the page's margins, not a gutter
        if Double(hi - lo) * width < gutterMin { continue }
        let cut = x0 + Double(lo + hi) / 2 * width
        let left = boxes.filter { ($0.x0 + $0.x1) / 2 < cut }.count
        if Swift.min(left, boxes.count - left) < Int(columnMinShare * Double(boxes.count)) {
            continue                                     // lopsided: a margin, not a column
        }
        cuts.append(cut)
    }
    return cuts
}

/// -> [[line index]] per column, left to right.
private func assignColumns(_ lines: [InkLayout.Line], _ cuts: [Double]) -> [[Int]] {
    var groups: [Int: [Int]] = [:]
    for (i, line) in lines.enumerated() {
        let centre = (line.box.x0 + line.box.x1) / 2
        let k = cuts.filter { centre >= $0 }.count
        groups[k, default: []].append(i)
    }
    return groups.keys.sorted().map { groups[$0]! }
}

/// Split one row into words at horizontal gaps.
///
/// Threshold is the larger of a fixed fraction of the row height and a multiple of the
/// row's own median gap, so it adapts to both cramped and airy handwriting.
private func wordsOf(_ indices: [Int], _ boxes: [InkBox], _ rowH: Double) -> [[Int]] {
    let idxs = stableSorted(indices, by: { boxes[$0].x0 })
    guard let first = idxs.first else { return [] }

    var gaps: [Double] = []
    var reach = boxes[first].x1
    for i in idxs.dropFirst() {
        gaps.append(Swift.max(0, boxes[i].x0 - reach))
        reach = Swift.max(reach, boxes[i].x1)
    }
    var thr = wordGapMin * rowH
    if !gaps.isEmpty { thr = Swift.max(thr, wordGapMedianFactor * median(gaps)) }

    var words: [[Int]] = []
    var cur = [first]
    reach = boxes[first].x1
    for i in idxs.dropFirst() {
        if boxes[i].x0 - reach > thr { words.append(cur); cur = [] }
        cur.append(i)
        reach = Swift.max(reach, boxes[i].x1)
    }
    words.append(cur)
    return words
}

/// Single-link cluster of line-start x positions -> one x per indent level.
///
/// Clustering rather than one global margin means an indented bullet list keeps its indent
/// instead of being dragged out to the body margin (SPEC §8.7, §17.2).
private func levelsOf(_ xs: [Double], _ tol: Double) -> [Double] {
    let sorted = xs.sorted()
    guard let first = sorted.first else { return [] }
    var out: [Double] = []
    var cur = [first]
    for x in sorted.dropFirst() {
        if x - (cur.last ?? x) > tol { out.append(median(cur)); cur = [] }
        cur.append(x)
    }
    out.append(median(cur))
    return out
}

// MARK: - planning helpers

/// Correct only the part of `err` that exceeds the deadband, so the response stays
/// continuous — no visible jump for a stroke sitting right on the threshold.
private func correct(_ err: Double, _ deadband: Double, _ gain: Double) -> Double {
    abs(err) <= deadband ? 0 : gain * (err - (err < 0 ? -deadband : deadband))
}

private func clamp(_ v: Double, _ cap: Double) -> Double { Swift.max(-cap, Swift.min(cap, v)) }

/// A lone bullet, dash or number that introduces a list item.
private func isMark(_ w: InkLayout.Word, _ refH: Double) -> Bool {
    w.box.width <= markMaxWidth * refH
}

/// Lenient by index: a missing or short `roles` array reads as prose everywhere.
private func roleLookup(_ roles: [Role]?) -> (Int) -> Role {
    { k in
        guard let roles, k >= 0, k < roles.count else { return .paragraph }
        return roles[k]
    }
}

/// New baseline per line. Ordering is preserved by construction.
///
/// A gap wider than `paraRatio x pitch` is read as a deliberate section break and left
/// alone — normalising every gap to one pitch would erase the page's structure. A heading
/// gets more room above and below it than body text does, and a frozen line simply keeps
/// the position it already had.
private func lineTargets(_ baselines: [Double], _ pitch: Double,
                         _ s: InkLayout.Strength, _ role: (Int) -> Role,
                         _ frozen: (Int) -> Bool) -> [Double] {
    guard let first = baselines.first else { return [] }
    var out = [first]
    let (dead, gain) = s.line
    for k in 1..<baselines.count {
        if frozen(k) { out.append(baselines[k]); continue }
        let gap = baselines[k] - baselines[k - 1]
        let lead = role(k) == .heading ? headingLead
            : (role(k - 1) == .heading ? headingTrail : 1.0)
        let want = gap > s.paraRatio * pitch ? gap : pitch * lead
        let new = gap + correct(want - gap, dead * pitch, gain)
        out.append((out.last ?? first) + Swift.max(new, minLineGap * pitch))
    }
    // A frozen line is an anchor the accumulation above it knows nothing about, so it is
    // the one way ordering can break: lines opened up higher on the page walk straight down
    // onto ink that never moves. Pull them back off it first (a no-op on an all-prose page,
    // where the forward accumulation already spaces every line by at least the floor), then
    // re-run the forward pass.
    for k in stride(from: out.count - 1, to: 0, by: -1) where !frozen(k - 1) {
        out[k - 1] = Swift.min(out[k - 1], out[k] - minLineGap * pitch)
    }
    for k in 1..<Swift.max(1, out.count) where !frozen(k) {   // never overtake the line above
        out[k] = Swift.max(out[k], out[k - 1] + minLineGap * pitch)
    }
    return out
}

/// Per indent level, where list text starts relative to the level. Median, so one badly
/// placed item cannot drag the whole list.
/// A list hangs off its own column's indent, so the offset is keyed by both.
struct BulletKey: Hashable { let block: Int; let level: Int }

private func bulletOffsets(_ a: InkLayout.Analysis,
                           _ role: (Int) -> Role) -> [BulletKey: Double] {
    var found: [BulletKey: [Double]] = [:]
    for (k, line) in a.lines.enumerated() where role(k) == .bullet {
        guard line.words.count >= 2, isMark(line.words[0], a.refH) else { continue }
        found[BulletKey(block: line.block, level: line.level), default: []]
            .append(line.words[1].box.x0 - line.levelX)
    }
    return found.mapValues(median)
}

/// Which correction to retire when a given measure gets worse.
let transformForMetric = ["baseline": "baseline", "pitch": "line",
                          "margin": "margin", "gap": "spacing"]

/// The same structure, re-measured on moved boxes. Membership is not recomputed.
///
/// Scoring a plan by re-analysing the result compares two different structures: on a dense
/// page the regrouping shifts a little and that churn shows up as a regression no
/// correction caused. Holding line and word membership fixed measures the thing actually
/// claimed — did *these* lines get tidier.
public func reproject(_ a: InkLayout.Analysis, _ boxes: [InkBox]) -> InkLayout.Analysis {
    var out = InkLayout.Analysis()
    out.refH = a.refH
    out.levels = a.levels
    out.blocks = a.blocks
    out.columns = a.columns
    out.wordGap = a.wordGap
    out.boxCount = boxes.count
    for line in a.lines {
        let words = line.words.map { w in
            InkLayout.Word(indices: w.indices,
                           box: InkBox.union(w.indices.map { boxes[$0] }),
                           baseline: baselineOf(w.indices, boxes, a.refH))
        }
        out.lines.append(InkLayout.Line(
            words: words,
            box: InkBox.union(line.indices.map { boxes[$0] }),
            baseline: baselineOf(line.indices, boxes, a.refH),
            level: line.level, levelX: line.levelX, block: line.block, isText: line.isText))
    }
    // pitch is re-derived: how evenly the lines now sit is exactly what is scored
    var diffs: [Double] = []
    for group in out.blocks {
        let rows = stableSorted(group) { out.lines[$0].baseline }.map { out.lines[$0] }
        diffs += zip(rows, rows.dropFirst())
            .map { $1.baseline - $0.baseline }.filter { $0 > 0 }
    }
    out.pitch = diffs.isEmpty ? a.pitch : median(diffs)
    return out
}

/// True if any measure got materially worse. Noise near zero does not count.
public func regressed(_ before: LayoutMetrics, _ after: LayoutMetrics,
                      _ refH: Double) -> String? {
    let pairs = [("baseline", before.baselineSpread, after.baselineSpread),
                 ("pitch", before.pitchSpread, after.pitchSpread),
                 ("margin", before.marginSpread, after.marginSpread),
                 ("gap", before.gapSpread, after.gapSpread)]
    for (name, was, now) in pairs where now > was * 1.05 && now - was > 0.05 * refH {
        return name
    }
    return nil
}

// MARK: - small utilities

/// Swift's `sort` is not guaranteed stable; the Python reference relies on stability for
/// tie-breaking (equal bottoms keep original stroke order), so ties fall back to position.
private func stableSorted<T>(_ items: [T], by key: (T) -> Double) -> [T] {
    items.enumerated().sorted { a, b in
        let (ka, kb) = (key(a.element), key(b.element))
        return ka == kb ? a.offset < b.offset : ka < kb
    }.map(\.element)
}

/// Must agree with Python's `round(v, n)` digit for digit: `describe` is the payload a
/// model sees and the cross-check diffs it against the reference. Scaling by 10^n and
/// rounding cannot do that — it rounds the scaled product, not the value, so 0.35 (whose
/// double is just *under* 0.35) scales to exactly 3.5 and rounds up to 0.4 where Python
/// gives 0.3. `%.*f` rounds the exact binary value half-to-even, which is Python's rule.
private func round(_ v: Double, _ places: Int) -> Double {
    Double(String(format: "%.\(places)f", v)) ?? v
}

#if DEBUG
/// Cheap invariants over a synthetic page. Empty result means everything held.
enum InkLayoutSelfCheck {
    /// Three rows of four two-stroke words, 20 pt tall, 40 pt apart, with a deterministic
    /// baseline wobble of +/- 2*jitter.
    private static func page(_ jitter: Double) -> [InkBox] {
        var out: [InkBox] = []
        for row in 0..<3 {
            let base = 100.0 + Double(row) * 40
            for w in 0..<4 {
                let x = 50.0 + Double(w) * 60
                let j = jitter * Double((row * 4 + w) % 5 - 2)
                out.append(InkBox(x0: x, y0: base - 20 + j, x1: x + 18, y1: base + j))
                out.append(InkBox(x0: x + 22, y0: base - 20 + j, x1: x + 40, y1: base + j))
            }
        }
        return out
    }

    static func run() -> [String] {
        var fail: [String] = []
        let boxes = page(2)
        let a = InkLayout.analyze(boxes)
        if a.lines.count != 3 { fail.append("rows: expected 3 lines, got \(a.lines.count)") }
        if a.lines.first?.words.count != 4 {
            fail.append("words: expected 4, got \(a.lines.first?.words.count ?? -1)")
        }

        let strong = InkLayout.plan(a, strength: .strong)
        let before = InkLayout.metrics(boxes, analysis: a)
        let after = InkLayout.metrics(InkLayout.moved(boxes, strong))
        if after.baselineSpread >= before.baselineSpread {
            fail.append("baseline: \(before.baselineSpread) -> \(after.baselineSpread)")
        }

        let travel = { (o: [Offset]) in o.reduce(0.0) { $0 + abs($1.dx) + abs($1.dy) } }
        let light = InkLayout.plan(a, strength: .light)
        if travel(light) >= travel(strong) {
            fail.append("strength: light \(travel(light)) not < strong \(travel(strong))")
        }

        let frozen = InkLayout.plan(a, strength: .strong, roles: [.paragraph, .equation, .paragraph])
        if a.lines.count > 1, a.lines[1].indices.contains(where: { !frozen[$0].isZero }) {
            fail.append("frozen: equation line moved")
        }

        if !InkLayout.plan(InkLayout.analyze([]), strength: .strong).isEmpty {
            fail.append("empty: expected no offsets")
        }
        return fail
    }
}
#endif

"""Recognised boxes -> groups of the user's own strokes.

The bridge. A recogniser says "there is a word about here"; this decides which original
records that sentence is talking about. Nothing here reads the recognised *text* for
geometry — the strokes assigned to a group supply every coordinate, so an OCR box that is
loose by a few points costs nothing.

    RecognizedLine[]  +  stroke boxes  ->  WordGroup[]  ->  layout.Analysis

Two rules keep it safe:

  * **A stroke belongs to at most one group.** Otherwise two groups would translate the
    same ink twice and tear it.
  * **A stroke that matches nothing is left out.** It then appears in no Word, so the
    planner emits no offset for it and it stays exactly where the user drew it. Diagrams,
    doodles and margin scribbles fall out here for free — the recogniser simply does not
    report them, and that is the correct answer, not a gap.
"""
from statistics import median

from . import layout


class WordGroup:
    """One recognised word, and the original strokes it is made of."""

    __slots__ = ("text", "indices", "box", "confidence", "line")

    def __init__(self, text, indices, box, confidence=0.0, line=0):
        self.text = text
        self.indices = indices        # indices into the caller's stroke box list
        self.box = box                # union of THOSE strokes, not the recogniser's box
        self.confidence = confidence
        self.line = line              # which recognised line it came from

    def __repr__(self):
        return f"<WordGroup {self.text!r} strokes={len(self.indices)}>"


# A recognised box is drawn around the letters, not around the ink: a descender, a long
# crossbar or the tail of a 'y' routinely sits outside it. Widened by this much of the
# line's own height before anything is tested against it.
SLOP_Y = 0.40
SLOP_X = 0.60


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def map_strokes(lines, boxes):
    """-> ([WordGroup] in reading order, [unmatched stroke index]).

    Assignment is per stroke rather than per box, which is what makes "at most one group"
    true by construction instead of by a later de-duplication pass.
    """
    bands = []
    for k, line in enumerate(lines):
        x0, y0, x1, y1 = line.box
        h = max(y1 - y0, 1e-6)
        bands.append((k, x0 - SLOP_X * h, y0 - SLOP_Y * h, x1 + SLOP_X * h, y1 + SLOP_Y * h,
                      (y0 + y1) / 2, h))

    claimed = {}          # stroke index -> (line index, word index)
    unmatched = []
    for i, s in enumerate(boxes):
        cx, cy = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
        best, best_score = None, None
        for k, bx0, by0, bx1, by1, mid, h in bands:
            if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
                continue
            # Nearest band centre wins. On tightly spaced writing a stroke can sit inside
            # two bands; the one it is centred in is the one it was written on.
            score = abs(cy - mid) / h
            if best_score is None or score < best_score:
                best, best_score = k, score
        if best is None:
            unmatched.append(i)
        else:
            claimed[i] = (best, _word_of(s, lines[best].words))

    groups = []
    for k, line in enumerate(lines):
        for w, word in enumerate(line.words):
            idx = [i for i, (lk, wk) in claimed.items() if lk == k and wk == w]
            if not idx:
                continue
            idx.sort()
            groups.append(WordGroup(text=word.text, indices=idx,
                                    box=layout._union([boxes[i] for i in idx]),
                                    confidence=word.confidence, line=k))
    return groups, unmatched


def _word_of(stroke, words):
    """Which word of a line a stroke belongs to. Horizontal only — the line already
    settled the vertical question, and within one line x is what separates words."""
    best, best_score = 0, -1.0
    for w, word in enumerate(words):
        score = _overlap(stroke[0], stroke[2], word.box[0], word.box[2])
        if score > best_score:
            best, best_score = w, score
    if best_score > 0:
        return best
    # No horizontal overlap at all (a stray accent past the end of a word): nearest centre.
    cx = (stroke[0] + stroke[2]) / 2
    return min(range(len(words)),
               key=lambda w: abs((words[w].box[0] + words[w].box[2]) / 2 - cx))


def analysis(groups, boxes, n_boxes=None):
    """[WordGroup] -> layout.Analysis, ready for the existing planner.

    The structure is the recogniser's; every number in it is measured off the original
    strokes. `n_boxes` keeps the offset array parallel to the caller's full stroke list,
    so unmatched strokes come back with a zero offset rather than disappearing.
    """
    a = layout.Analysis(n_boxes=len(boxes) if n_boxes is None else n_boxes)
    if not groups:
        return a

    grouped = [i for g in groups for i in g.indices]
    a.ref_h = layout._ref_height([boxes[i] for i in grouped])

    by_line = {}
    for g in groups:
        by_line.setdefault(g.line, []).append(g)

    # Same correction `analyze` makes, and for the same reason: a per-stroke estimate is
    # badly low on print-style or mathematical writing, where most records are a dot, a
    # bar or an exponent. A recognised line IS a line of writing, so its height is the
    # honest number — and here it comes from a recogniser rather than from clustering.
    heights = [h for h in (layout._union([g.box for g in gs])[3]
                           - layout._union([g.box for g in gs])[1]
                           for gs in by_line.values() if len(gs) > 1) if h > 0]
    if heights:
        a.ref_h = max(a.ref_h, min(median(heights), 4.0 * a.ref_h))

    lines = []
    for _, gs in sorted(by_line.items()):
        gs.sort(key=lambda g: g.box[0])
        words = [layout.Word(indices=g.indices, box=g.box,
                             baseline=layout._baseline(g.indices, boxes, a.ref_h))
                 for g in gs]
        idx = [i for g in gs for i in g.indices]
        lines.append(layout.Line(words=words, box=layout._union([g.box for g in gs]),
                                 baseline=layout._baseline(idx, boxes, a.ref_h)))
    a.lines = sorted(lines, key=lambda l: l.baseline)

    for line in a.lines:
        # Recognised as text, so it is text — that judgement is the recogniser's whole
        # job, and it is a far better one than the width-to-height ratio geometry uses.
        line.is_text = True
        # Stacked maths still has to be protected. A recogniser reads a fraction as one
        # line and will happily let the planner re-space its numerator away from its
        # denominator, so this check earns its keep even here.
        line.rigid = layout._is_stacked(line, boxes, a.ref_h)
        if line.rigid:
            idx = line.indices
            line.words = [layout.Word(indices=idx, box=line.box,
                                      baseline=layout._baseline(idx, boxes, a.ref_h))]
    return _fuse_stacked(layout.statistics(a, boxes), boxes)


# A baseline step smaller than this share of the page's own pitch is not a line step. The
# distribution on a real page of maths is cleanly bimodal — p25 of the within-column gaps
# sat at 8.4pt against a 9.1pt pitch, while the bottom tenth sat at 5.4pt and below — so
# this sits in the empty part of it rather than on a slope.
STACK_PITCH = 0.55


def _fuse_stacked(a, boxes):
    """Fuse lines that sit too close together to be separate lines. -> the same Analysis.

    `recognize.merge_stacked` joins readings that *overlap*. It cannot join a numerator
    sitting cleanly above its denominator, because from two boxes alone that is
    indistinguishable from two lines of prose — and welding prose together would be worse.

    Once the page has been measured, though, it is distinguishable: two readings a fifth of
    a pitch apart, in the same column, are one line of writing with something stacked in
    it. Left as two, line spacing pushes them to a full pitch apart, which tears a fraction
    in half and drives the halves into the lines above and below — the failure this exists
    to stop, seen on a real page before it did.

    The fused line is rigid: it translates whole, and nothing re-spaces inside it.
    """
    if not a.lines or a.pitch <= 0:
        return a
    limit = STACK_PITCH * a.pitch
    # Within a column only. Two columns' lines interleave by baseline, and their spacing
    # says nothing about either.
    fuse = {}                       # line index -> the index it joins
    for group in a.blocks:
        rows = sorted(group, key=lambda k: a.lines[k].baseline)
        for prev, cur in zip(rows, rows[1:]):
            if a.lines[cur].baseline - a.lines[prev].baseline < limit:
                fuse[cur] = fuse.get(prev, prev)
    if not fuse:
        return a

    merged, keep = {}, []
    for k, line in enumerate(a.lines):
        root = k
        while root in fuse:
            root = fuse[root]
        merged.setdefault(root, []).append(line)
        if root == k:
            keep.append(k)

    out = layout.Analysis(n_boxes=a.n_boxes, ref_h=a.ref_h)
    for k in keep:
        parts = merged[k]
        if len(parts) == 1:
            out.lines.append(parts[0])
            continue
        idx = [i for p in parts for w in p.words for i in w.indices]
        box = layout._union([p.box for p in parts])
        out.lines.append(layout.Line(
            words=[layout.Word(indices=idx, box=box,
                               baseline=layout._baseline(idx, boxes, a.ref_h))],
            box=box, baseline=layout._baseline(idx, boxes, a.ref_h),
            is_text=True, rigid=True))
    out.lines.sort(key=lambda l: l.baseline)
    # Re-measured, not carried over: fusing changes the line count, so pitch, indent levels
    # and the word gap all have to be taken again or they describe the old structure.
    return layout.statistics(out, boxes)


def coverage(groups, boxes):
    """-> (strokes grouped, total, share). The one number that says whether recognition
    actually happened, and the first thing to look at when a page comes out unchanged."""
    n = sum(len(g.indices) for g in groups)
    return n, len(boxes), (n / len(boxes) if boxes else 0.0)


def summary(lines, groups, boxes):
    n, total, share = coverage(groups, boxes)
    widths = [g.box[2] - g.box[0] for g in groups] or [0.0]
    return (f"{len(lines)} lines, {len(groups)} words, "
            f"{n}/{total} strokes grouped ({share:.0%}), "
            f"median word width {median(widths):.1f}pt")

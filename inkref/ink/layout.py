"""Structure detection and the layout plan. Pure geometry — knows no file format.

Input is a list of stroke bounding boxes. Output is one `(dx, dy)` per stroke.

Everything this module produces is a **translation**. Nothing here scales, rotates or
regenerates a stroke, so whatever the caller applies the offsets to keeps its own shape
exactly. That is the product's core promise (SPEC §7) and it is also what makes the
GoodNotes side safe: translating a record is the one edit confirmed in the app to leave
ink lasso-selectable, erasable and undeformed (FINDINGS, milestone 1).

Pipeline:

    boxes -> rows (lines) -> words -> baselines, pitch, indent levels, word gap
          -> per-word (dx, dy)

Thresholds are expressed as multiples of `ref_h`, the page's median stroke height, so the
same numbers work at any pen size or page scale.
"""
import math
from bisect import insort
from dataclasses import dataclass, field
from statistics import median

# --- tunables, all in multiples of ref_h unless noted ---------------------------------
ROW_BASELINE_TOL = 0.45     # a stroke joins a row if its bottom is this close to it
ROW_OVERLAP = 0.50          # ...or if it vertically overlaps the row this much
# ...and in either case it must also be horizontally near the row. Without this a row is
# defined by height alone, so two strokes on opposite edges of a multi-column page join the
# same "line" — which is how a 4-column page of notes came out as 155 lines, one of them
# 40 words wide, spanning every column. A word gap is well under 2x the writing height;
# anything past this is a different block of text.
ROW_MAX_GAP = 5.0
WORD_GAP_MIN = 0.55         # a gap must beat this (and 1.8x the median gap) to split words
WORD_GAP_MEDIAN_FACTOR = 1.8
# ponytail: single-link clustering of line starts. Fine when indents are a clear step
# apart; if drift approaches the indent step the levels merge and lines are pulled to the
# body margin — conservative, but a mode-seeking or gap-statistic clustering would do better.
INDENT_TOL = 0.90           # line starts within this of each other are one indent level
MIN_LINE_GAP = 0.35         # of pitch — line spacing may never collapse below this
PARA_RATIO = 1.70           # a line gap wider than this x pitch reads as a section break
TALL = 0.45                 # a stroke this tall relative to ref_h actually sits on the baseline
HEADING_LEAD = 1.35         # of pitch — room opened above a heading
HEADING_TRAIL = 1.15        # ...and below it
MARK_MAX_WIDTH = 0.90       # a first word narrower than this may be a bullet mark

# A row only counts as writing if it is much wider than it is tall. Measured on real ink:
# lines of handwriting run 11-22x, while the strokes of a drawing group into rows of
# 0.7-1.7x. Nothing sits in between, so the threshold is not a fine judgement.
#
# This is the safety net that keeps a sketch, a diagram or a doodle from being aligned to a
# text baseline it never belonged to (SPEC §15: prefer leaving unsupported structures
# unchanged). It is pure geometry and needs no classifier, so it holds with AI switched
# off — and a model may freeze more, never less.
TEXT_ASPECT = 3.0
MIN_TEXT_LINES = 2          # below this a page is not a page of writing; leave it alone
COLUMN_QUIET = 0.08         # a gutter bin carries at most this share of the peak coverage
COLUMN_GUTTER = 1.50        # ...and the quiet band must be this wide, x ref_h
COLUMN_MIN_SHARE = 0.10     # both sides of a cut must hold this share of the strokes

# Semantic roles. Geometry can guess some of these; a vision model can do better (see
# inkref/ai/). Either way the role only ever selects which deterministic rule applies —
# the numbers below, and the transform itself, stay ours.
PARAGRAPH, HEADING, BULLET, EQUATION, DIAGRAM = (
    "paragraph", "heading", "bullet", "equation", "diagram")

# Roles whose ink is never moved. Aligning a formula or a sketch to a text baseline would
# wreck it, and "leave it alone" is always a safe answer.
FROZEN_ROLES = frozenset({EQUATION, DIAGRAM})


@dataclass(frozen=True)
class Strength:
    """A (deadband, gain) pair per transform.

    deadband: error smaller than this is left alone, so natural variation survives.
    gain:     fraction of the error *beyond* the deadband that is corrected.

    Only the excess is corrected, which keeps the response continuous — no visible jump
    for a stroke that happens to sit right on the threshold.
    """
    name: str
    baseline: tuple      # (deadband x ref_h, gain)
    line: tuple          # (deadband x pitch, gain)
    margin: tuple        # (deadband x ref_h, gain)
    spacing: tuple       # (deadband x ref_h, gain)
    para_ratio: float    # a line gap wider than this x pitch is a deliberate break
    max_shift: float     # hard cap on |dx| and |dy|, x ref_h


# Deadbands are sized against the defects they exist to tolerate, not picked to look
# cautious: handwriting wobbles off its baseline by roughly 0.1-0.2 of the writing height,
# drifts off a margin by 0.3-0.6, and varies word gaps by 0.3-0.5. A deadband set above
# that band does nothing at all, which is how these were first (wrongly) tuned.
LIGHT = Strength("light", (0.18, 0.55), (0.28, 0.50), (0.50, 0.50), (0.45, 0.45), 1.8, 2.0)
BALANCED = Strength("balanced", (0.06, 0.85), (0.10, 0.80), (0.18, 0.80), (0.18, 0.75), 1.7, 4.0)
STRONG = Strength("strong", (0.02, 1.00), (0.03, 1.00), (0.05, 1.00), (0.05, 1.00), 1.6, 6.0)

STRENGTHS = {s.name: s for s in (LIGHT, BALANCED, STRONG)}


def strength(value):
    """Accept a name or a Strength."""
    if isinstance(value, Strength):
        return value
    try:
        return STRENGTHS[str(value).lower()]
    except KeyError:
        raise ValueError(f"unknown strength {value!r}; try {sorted(STRENGTHS)}") from None


# --- structure ------------------------------------------------------------------------
@dataclass
class Word:
    indices: list        # indices into the caller's box list
    box: tuple           # x0, y0, x1, y1
    baseline: float      # median stroke bottom


@dataclass
class Line:
    words: list
    box: tuple
    baseline: float
    level: int = 0            # index into Analysis.levels, for previews and describe()
    level_x: float = 0.0      # the x this line's indent is measured against
    block: int = 0            # which column/text block it belongs to
    is_text: bool = True      # False = a drawing row; never moved, never a statistic

    @property
    def indices(self):
        return [i for w in self.words for i in w.indices]


@dataclass
class Analysis:
    lines: list = field(default_factory=list)
    ref_h: float = 1.0
    pitch: float = 1.0
    levels: list = field(default_factory=list)   # x of each indent level
    blocks: list = field(default_factory=list)   # [[line index]], columns, left to right
    columns: list = field(default_factory=list)  # x of each column separator
    word_gap: float = 0.0                        # target gap between words
    n_boxes: int = 0

    @property
    def words(self):
        return [w for line in self.lines for w in line.words]

    @property
    def text_lines(self):
        return [l for l in self.lines if l.is_text]


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _ref_height(boxes):
    """Estimate the writing height from stroke boxes.

    A plain median is wrong here: an 'A' crossbar, an 'i' dot and a hyphen are separate
    strokes of nearly zero height, and in a page of print-style handwriting they can
    outnumber the full-height ones. Take a near-maximum first, then the median of the
    strokes within reach of it — the letters that actually span the writing height.
    """
    hs = sorted(h for h in (b[3] - b[1] for b in boxes) if h > 0)
    if not hs:
        return 1.0
    big = hs[min(len(hs) - 1, int(len(hs) * 0.9))]     # robust near-max
    tall = [h for h in hs if h >= 0.35 * big]
    return median(tall) if tall else big


def _baseline(idxs, boxes, ref_h):
    """Where a group of strokes sits. Bars and dots are excluded for the same reason."""
    bottoms = [boxes[i][3] for i in idxs
               if boxes[i][3] - boxes[i][1] >= TALL * ref_h]
    return median(bottoms or [boxes[i][3] for i in idxs])


def _rows(boxes, ref_h):
    """Group stroke indices into text rows.

    Two passes, and the order matters. Full-height strokes seed the rows first, sorted by
    bottom edge — a baseline is what a row actually shares, and a bottom edge barely moves
    where a y-centre swings with ascenders. Only then are the short strokes attached: a
    crossbar, a dot, a hyphen, the top bar of a `T`. Those carry no baseline of their own,
    and seeding a row with one puts a row 22 pt above the text it belongs to, which then
    refuses every real letter that arrives after it.
    """
    tall = [i for i, b in enumerate(boxes) if b[3] - b[1] >= TALL * ref_h]
    if not tall:                                   # a page of dots; nothing better to do
        tall = list(range(len(boxes)))
    short = [i for i in range(len(boxes)) if i not in set(tall)]

    max_gap = ROW_MAX_GAP * ref_h

    # Each row carries running aggregates instead of being re-measured. Recomputing a row's
    # baseline and extent from scratch for every candidate makes matching
    # O(strokes x rows x row size) — about 115M operations on a 10,000-stroke page, which
    # was 2.3 of the 2.7 seconds it took to lay one out. The bottoms are kept sorted so the
    # median is an index rather than a sort, and the numbers are therefore identical to the
    # naive version, not an approximation of it.
    rows = []          # _Row

    class _Row:
        __slots__ = ("indices", "y0", "y1", "tall_bottoms", "all_bottoms")

        def __init__(self, i, box, is_tall):
            self.indices = [i]
            self.y0, self.y1 = box[1], box[3]
            self.all_bottoms = [box[3]]
            self.tall_bottoms = [box[3]] if is_tall else []

        def add(self, i, box, is_tall):
            self.indices.append(i)
            self.y0 = min(self.y0, box[1])
            self.y1 = max(self.y1, box[3])
            insort(self.all_bottoms, box[3])
            if is_tall:
                insort(self.tall_bottoms, box[3])

        @property
        def baseline(self):
            v = self.tall_bottoms or self.all_bottoms
            m = len(v) // 2
            return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2

    for i in sorted(tall, key=lambda i: boxes[i][3]):
        x0, y0, x1, y1 = boxes[i]
        best, best_err = None, None
        for row in rows:
            rb = row.baseline
            overlap = min(y1, row.y1) - max(y0, row.y0)
            h = min(y1 - y0, row.y1 - row.y0)
            fits = (abs(y1 - rb) <= ROW_BASELINE_TOL * ref_h
                    or (h > 0 and overlap / h >= ROW_OVERLAP))
            if fits and (best_err is None or abs(y1 - rb) < best_err):
                best, best_err = row, abs(y1 - rb)
        (rows.append(_Row(i, boxes[i], True)) if best is None
         else best.add(i, boxes[i], True))

    for i in sorted(short, key=lambda i: boxes[i][3]):
        x0, y0, x1, y1 = boxes[i]
        best, best_score = None, 0.0
        for row in rows:
            score = min(y1, row.y1) - max(y0, row.y0)    # vertical overlap, in points
            if score > best_score:
                best, best_score = row, score
        if best is None:                                 # not inside any row: nearest one
            cy = (y0 + y1) / 2
            near = [(abs(cy - r.baseline), r) for r in rows]
            near = [(d, r) for d, r in near if d <= 1.5 * ref_h]
            best = min(near, key=lambda t: t[0])[1] if near else None
        (rows.append(_Row(i, boxes[i], False)) if best is None
         else best.add(i, boxes[i], False))

    rows = [r.indices for r in rows]

    # Only now split a row where it crosses a wide horizontal gap. Doing it while building
    # rows makes the result depend on the order strokes arrive in — a stroke at the far
    # right is compared against a row that so far holds only its left half, and gets thrown
    # into a row of its own. Splitting a finished row cannot go wrong that way.
    out = []
    for row in rows:
        row.sort(key=lambda i: boxes[i][0])
        piece, reach = [row[0]], boxes[row[0]][2]
        for i in row[1:]:
            if boxes[i][0] - reach > max_gap:
                out.append(piece)
                piece = []
            piece.append(i)
            reach = max(reach, boxes[i][2])
        out.append(piece)
    return out


def _words(indices, boxes, row_h):
    """Split one row into words at horizontal gaps.

    Threshold is the larger of a fixed fraction of the row height and a multiple of the
    row's own median gap, so it adapts to both cramped and airy handwriting.
    """
    idxs = sorted(indices, key=lambda i: boxes[i][0])
    gaps = []
    reach = boxes[idxs[0]][2]
    for i in idxs[1:]:
        gaps.append(max(0.0, boxes[i][0] - reach))
        reach = max(reach, boxes[i][2])
    thr = WORD_GAP_MIN * row_h
    if gaps:
        thr = max(thr, WORD_GAP_MEDIAN_FACTOR * median(gaps))

    words, cur = [], [idxs[0]]
    reach = boxes[idxs[0]][2]
    for i in idxs[1:]:
        if boxes[i][0] - reach > thr:
            words.append(cur)
            cur = []
        cur.append(i)
        reach = max(reach, boxes[i][2])
    words.append(cur)
    return words


def _columns(boxes, ref_h):
    """-> x positions that separate the page's columns, left to right. Empty = one column.

    Every vertical rule — line pitch, section breaks, ordering — is meaningless across two
    columns that merely happen to sit at the same height. A four-column page sorted by
    baseline interleaves all four, and the measured "line spacing" becomes the distance
    between neighbouring columns instead: 0.4pt on real notes whose lines are 8pt apart.

    Found by vertical projection. Chaining lines by x-overlap does not work — one wide line
    spanning two columns links them, and transitively the whole page collapses into a
    single block, which is exactly what it did. A gutter is instead a band that is quiet
    down the *entire* page, which no single line can forge.

    Fully empty gutters are rare (a graph or a long formula leaks across), so the test is
    near-quiet rather than empty, and a cut is only taken when both sides hold a real share
    of the ink.
    """
    if len(boxes) < 4 * COLUMN_MIN_SHARE ** -1:
        return []
    x0 = min(b[0] for b in boxes)
    x1 = max(b[2] for b in boxes)
    span = x1 - x0
    gutter_min = max(COLUMN_GUTTER * ref_h, 1e-9)
    if span <= 4 * gutter_min:
        return []

    bins = max(16, int(span / max(ref_h * 0.5, 1e-6)))
    width = span / bins
    cover = [0] * bins
    for b in boxes:
        lo = min(bins - 1, max(0, int((b[0] - x0) / width)))
        hi = min(bins - 1, max(0, int((b[2] - x0) / width)))
        for i in range(lo, hi + 1):
            cover[i] += 1
    peak = max(cover)
    if not peak:
        return []

    quiet = peak * COLUMN_QUIET
    runs, start = [], None
    for i, c in enumerate(cover):
        if c <= quiet and start is None:
            start = i
        elif c > quiet and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, bins))

    cuts = []
    for lo, hi in runs:
        if lo == 0 or hi == bins:           # the page's own margins, not a gutter
            continue
        if (hi - lo) * width < gutter_min:
            continue
        cut = x0 + (lo + hi) / 2 * width
        left = sum(1 for b in boxes if (b[0] + b[2]) / 2 < cut)
        if min(left, len(boxes) - left) < COLUMN_MIN_SHARE * len(boxes):
            continue                        # a lopsided split is a margin, not a column
        cuts.append(cut)
    return cuts


def _assign_columns(lines, cuts):
    """-> [[line index]] per column, left to right, each sorted by baseline."""
    groups = {}
    for i, line in enumerate(lines):
        centre = (line.box[0] + line.box[2]) / 2
        k = sum(1 for c in cuts if centre >= c)
        groups.setdefault(k, []).append(i)
    return [groups[k] for k in sorted(groups)]


def _levels(xs, tol):
    """Single-link cluster of line-start x positions -> one x per indent level.

    Clustering rather than one global margin means an indented bullet list keeps its
    indent instead of being dragged out to the body margin (SPEC §8.7, §17.2).
    """
    if not xs:
        return []
    out, cur = [], [sorted(xs)[0]]
    for x in sorted(xs)[1:]:
        if x - cur[-1] > tol:
            out.append(median(cur))
            cur = []
        cur.append(x)
    out.append(median(cur))
    return out


def analyze(boxes):
    """boxes: [(x0, y0, x1, y1)] -> Analysis. Empty input gives an empty Analysis."""
    boxes = [tuple(map(float, b)) for b in boxes]
    a = Analysis(n_boxes=len(boxes))
    if not boxes:
        return a

    # Two passes. A first estimate from individual strokes is biased low, badly so on
    # print-style or mathematical writing where most records are sub-character fragments —
    # a dot, a bar, an exponent — and only a few span the writing height. Real notes
    # measured a stroke median of 1.7pt against a true writing height near 6pt. Grouping
    # once gives rows, and a row IS a line of writing, so its height is the honest number;
    # regrouping with it fixes both the row tolerance and every threshold downstream.
    a.ref_h = _ref_height(boxes)
    first = _rows(boxes, a.ref_h)
    heights = [_union([boxes[i] for i in row])[3] - _union([boxes[i] for i in row])[1]
               for row in first if len(row) > 1]
    if heights:
        refined = median(heights)
        # Only ever trust it upward: rows can merge across blocks and overstate nothing,
        # but a single-stroke row cannot understate the writing height either.
        a.ref_h = max(a.ref_h, min(refined, 4.0 * a.ref_h))

    lines = []
    for row in _rows(boxes, a.ref_h):
        row_h = _ref_height([boxes[i] for i in row]) or a.ref_h
        words = [Word(indices=w, box=_union([boxes[i] for i in w]),
                      baseline=_baseline(w, boxes, a.ref_h))
                 for w in _words(row, boxes, row_h)]
        lines.append(Line(words=words,
                          box=_union([boxes[i] for i in row]),
                          baseline=_baseline(row, boxes, a.ref_h)))
    a.lines = sorted(lines, key=lambda l: l.baseline)
    for line in a.lines:
        h = line.box[3] - line.box[1]
        line.is_text = h > 0 and (line.box[2] - line.box[0]) >= TEXT_ASPECT * h

    # Every statistic below is taken from writing only. One tall drawing dropped into a
    # page of notes would otherwise drag the pitch, the margin and the word gap with it,
    # and the text would be aligned to a shape that is not text.
    text = a.text_lines
    a.columns = _columns(boxes, a.ref_h)
    a.blocks = [[k for k in g if a.lines[k].is_text]
                for g in _assign_columns(a.lines, a.columns)]
    a.blocks = [g for g in a.blocks if g]
    for n, group in enumerate(a.blocks):
        group.sort(key=lambda k: a.lines[k].baseline)
        for k in group:
            a.lines[k].block = n

    # Pitch and indent levels are per block: a column has its own line rhythm and its own
    # left edge, and mixing two columns' worth of either produces a number that describes
    # neither.
    diffs = []
    levels = []
    for group in a.blocks:
        rows = [a.lines[k] for k in group]
        diffs += [b.baseline - t.baseline for t, b in zip(rows, rows[1:])
                  if b.baseline - t.baseline > 0]
        local = _levels([l.box[0] for l in rows], INDENT_TOL * a.ref_h) or [rows[0].box[0]]
        for line in rows:
            line.level_x = min(local, key=lambda x: abs(x - line.box[0]))
        levels += local
    a.pitch = median(diffs) if diffs else a.ref_h * 1.6

    a.levels = sorted(set(levels)) or [0.0]
    for line in a.lines:
        line.level = min(range(len(a.levels)),
                         key=lambda k: abs(a.levels[k] - line.level_x))

    gaps = [line.words[k + 1].box[0] - line.words[k].box[2]
            for line in text for k in range(len(line.words) - 1)]
    gaps = [g for g in gaps if g > 0]
    target = median(gaps) if gaps else 0.6 * a.ref_h
    a.word_gap = min(max(target, 0.40 * a.ref_h), 1.50 * a.ref_h)
    return a


# --- planning -------------------------------------------------------------------------
def _correct(err, deadband, gain):
    """Correct only the part of `err` that exceeds the deadband."""
    if abs(err) <= deadband:
        return 0.0
    return gain * (err - math.copysign(deadband, err))


def _line_targets(baselines, pitch, s, role, frozen):
    """New baseline per line. Ordering is preserved by construction.

    A gap wider than `para_ratio x pitch` is read as a deliberate section break and left
    alone — normalising every gap to one pitch would erase the page's structure. A heading
    gets more room above and below it than body text does, and a frozen line simply keeps
    the position it already had.
    """
    if not baselines:
        return []
    out = [baselines[0]]
    dead, gain = s.line
    for k in range(1, len(baselines)):
        if frozen(k):
            out.append(baselines[k])
            continue
        gap = baselines[k] - baselines[k - 1]
        lead = (HEADING_LEAD if role(k) == HEADING
                else HEADING_TRAIL if role(k - 1) == HEADING else 1.0)
        want = gap if gap > s.para_ratio * pitch else pitch * lead
        new = gap + _correct(want - gap, dead * pitch, gain)
        out.append(out[-1] + max(new, MIN_LINE_GAP * pitch))
    # A frozen line is an anchor the accumulation above it knows nothing about, so it is
    # the one way ordering can break: lines opened up higher on the page walk straight down
    # onto ink that never moves. Pull them back off it first (a no-op on an all-prose page,
    # where the forward accumulation already spaces every line by at least the floor), then
    # re-run the forward pass.
    for k in range(len(out) - 1, 0, -1):
        if not frozen(k - 1):
            out[k - 1] = min(out[k - 1], out[k] - MIN_LINE_GAP * pitch)
    for k in range(1, len(out)):                      # never overtake the line above
        if not frozen(k):
            out[k] = max(out[k], out[k - 1] + MIN_LINE_GAP * pitch)
    return out


def _is_mark(word, ref_h):
    """A lone bullet, dash or number that introduces a list item."""
    return (word.box[2] - word.box[0]) <= MARK_MAX_WIDTH * ref_h


def _bullet_offsets(a, roles):
    """Per indent level, where list text starts relative to the level. Median, so one
    badly placed item cannot drag the whole list."""
    found = {}
    for line, role in zip(a.lines, roles):
        if role == BULLET and len(line.words) >= 2 and _is_mark(line.words[0], a.ref_h):
            found.setdefault((line.block, line.level), []).append(
                line.words[1].box[0] - line.level_x)
    return {lvl: median(v) for lvl, v in found.items()}


def plan(a, s=BALANCED, roles=None, skip=()):
    """Analysis -> [(dx, dy)] parallel to the original box list.

    All translations, composed per word:
      line spacing   (SPEC §8.6)  vertical, whole line
      baseline align (SPEC §8.4)  vertical, per word within its line
      margin align   (SPEC §8.7)  horizontal, whole line, toward its indent level
      word spacing   (SPEC §8.5)  horizontal, cumulative along the line

    `roles` is one role per line, in `a.lines` order — usually from `inkref.ai`, and
    `None` means treat everything as prose. A role never supplies a coordinate; it only
    chooses which of the rules above apply, which is the whole point of the split.

    `skip` switches individual corrections off by name — "baseline", "line", "margin",
    "spacing". A page can be well served by three of them and hurt by the fourth, and
    abandoning the whole plan over one throws away the other three (SPEC §8.4-§8.7 are
    separate promises, not a bundle).
    """
    s = strength(s)
    offsets = [(0.0, 0.0)] * a.n_boxes
    if not a.lines:
        return offsets
    roles = list(roles) if roles else [PARAGRAPH] * len(a.lines)
    if len(roles) != len(a.lines):
        raise ValueError(f"got {len(roles)} roles for {len(a.lines)} lines")

    # Too little writing to reason about. A page that is mostly drawing has no baseline
    # grid, no margin and no pitch worth inferring, and guessing one wrecks the page.
    if len(a.text_lines) < MIN_TEXT_LINES:
        return offsets

    def frozen(k):
        # Geometry can veto; a role can only add to the veto. A classifier calling a
        # sketch a paragraph must not license moving it.
        return roles[k] in FROZEN_ROLES or not a.lines[k].is_text

    # Line spacing is resolved inside each column. Running it over the page-ordered list
    # would space a line against whichever column happened to sit beside it.
    targets = [l.baseline for l in a.lines]
    for group in ([] if "line" in skip else a.blocks):
        local = _line_targets([a.lines[k].baseline for k in group], a.pitch, s,
                              lambda i: roles[group[i]], lambda i: frozen(group[i]))
        for i, k in enumerate(group):
            targets[k] = local[i]
    bullets = _bullet_offsets(a, roles)
    cap = s.max_shift * a.ref_h
    base_dead, base_gain = (0.0, 0.0) if 'baseline' in skip else s.baseline
    marg_dead, marg_gain = (0.0, 0.0) if 'margin' in skip else s.margin
    sp_dead, sp_gain = (0.0, 0.0) if 'spacing' in skip else s.spacing

    for k, (line, target, role) in enumerate(zip(a.lines, targets, roles)):
        if frozen(k):
            continue                                   # keeps (0.0, 0.0): never touched
        ldy = target - line.baseline
        level_x = line.level_x
        ldx = _correct(level_x - line.box[0], marg_dead * a.ref_h, marg_gain)
        listed = (role == BULLET and (line.block, line.level) in bullets
                  and len(line.words) >= 2 and _is_mark(line.words[0], a.ref_h))

        shift = 0.0
        prev_right = None
        for wi, w in enumerate(line.words):
            if prev_right is not None:
                if listed and wi == 1:
                    # hang the item text off a shared offset instead of a generic gap,
                    # so a list reads as a column (SPEC §17.2)
                    want = level_x + bullets[(line.block, line.level)]
                    shift = _correct(want - (w.box[0] + ldx), sp_dead * a.ref_h, sp_gain)
                else:
                    gap = w.box[0] - prev_right
                    shift += _correct(a.word_gap - gap, sp_dead * a.ref_h, sp_gain)
            prev_right = w.box[2]

            # A word too short to reach the baseline — a hyphen, a dot, an accent — has no
            # baseline of its own to trust, so it rides with its line and nothing else.
            wdy = 0.0
            if w.box[3] - w.box[1] >= TALL * a.ref_h:
                wdy = _correct(line.baseline - w.baseline, base_dead * a.ref_h, base_gain)

            dx = _clamp(ldx + shift, cap)
            dy = _clamp(ldy + wdy, cap)
            for i in w.indices:
                offsets[i] = (dx, dy)
    return offsets


def _clamp(v, cap):
    return max(-cap, min(cap, v))


def reproject(a, boxes):
    """The same structure, re-measured on moved boxes. Membership is not recomputed.

    Scoring a plan by re-analysing the result compares two different structures: on a
    dense page the regrouping shifts a little, and that churn shows up as a regression
    that no correction caused. Keeping the line and word membership fixed and only
    recomputing where those lines now sit measures the thing actually claimed — did *these*
    lines get tidier — and makes the before/after comparison like-for-like.
    """
    out = Analysis(ref_h=a.ref_h, levels=list(a.levels), blocks=[list(g) for g in a.blocks],
                   columns=list(a.columns), word_gap=a.word_gap, n_boxes=len(boxes))
    for line in a.lines:
        words = [Word(indices=list(w.indices),
                      box=_union([boxes[i] for i in w.indices]),
                      baseline=_baseline(w.indices, boxes, a.ref_h))
                 for w in line.words]
        out.lines.append(Line(words=words,
                              box=_union([boxes[i] for i in line.indices]),
                              baseline=_baseline(line.indices, boxes, a.ref_h),
                              level=line.level, level_x=line.level_x,
                              block=line.block, is_text=line.is_text))
    # pitch is re-derived, because how evenly the lines now sit is exactly what is scored
    diffs = []
    for group in out.blocks:
        rows = sorted((out.lines[k] for k in group), key=lambda l: l.baseline)
        diffs += [b.baseline - t.baseline for t, b in zip(rows, rows[1:])
                  if b.baseline - t.baseline > 0]
    out.pitch = median(diffs) if diffs else a.pitch
    return out


# Which correction to retire when a given measure gets worse.
TRANSFORM_FOR_METRIC = {
    "baseline_spread": "baseline",
    "pitch_spread": "line",
    "margin_spread": "margin",
    "gap_spread": "spacing",
}


def regressed(before, after, ref_h):
    """True if any measure got materially worse. Noise near zero does not count."""
    for key, was in before.items():
        now = after.get(key, 0.0)
        if now > was * 1.05 and now - was > 0.05 * ref_h:
            return key
    return None


def verified_plan(a, boxes, s=BALANCED, roles=None):
    """-> (offsets, strength_used, regression). A plan that is measured before it is kept.

    Structure detection is a guess, and on a page it reads badly — a dense multi-column
    formula sheet, say — a confident plan makes the page worse. The metrics that judge the
    result cost nothing to compute against the moved boxes, so the plan is scored before it
    is handed back, and a plan that loses is replaced by a gentler one and then by no plan
    at all.

    Doing nothing is always available and always safe. For a tool that edits someone's
    notes, never making a page worse is worth more than squeezing out the last alignment.
    """
    s = strength(s)
    before = metrics(boxes, a, roles)
    skip = set()
    hurt = None
    for candidate in ([s] if s is LIGHT else [s, s, LIGHT]):
        offsets = plan(a, candidate, roles, skip=skip)
        shifted = moved(boxes, offsets)
        after = metrics(shifted, reproject(a, shifted), roles)
        hurt = regressed(before, after, a.ref_h)
        if hurt is None:
            return offsets, candidate, None
        # Retire only the correction that did the damage and try again. The four are
        # separate promises, and a page can be well served by three of them while the
        # fourth is wrong for it — a dense formula sheet gains a straight left margin
        # even where its line rhythm is too irregular to normalise. Dropping the whole
        # plan over one bad measure throws the other three away.
        offender = TRANSFORM_FOR_METRIC.get(hurt)
        if offender and offender not in skip:
            skip.add(offender)
        else:
            skip = set()          # nothing left to retire; fall through to a gentler pass
    return [(0.0, 0.0)] * a.n_boxes, None, hurt


def beautify(boxes, s=BALANCED, roles=None):
    """-> (Analysis, offsets). The whole engine in one call."""
    a = analyze(boxes)
    return a, verified_plan(a, boxes, s, roles)[0]


def describe(a):
    """The page as a classifier sees it: one record per detected line, geometry only.

    This is the entire payload a model is given about structure — ids, boxes and a few
    ratios. It is deliberately not asked where anything should go (SPEC: the model decides
    *what*, this engine decides *where*), and every id it may answer with appears here, so
    an answer naming anything else is provably invented and gets dropped.
    """
    out = []
    for k, line in enumerate(a.lines):
        x0, y0, x1, y1 = line.box
        out.append({
            "id": f"L{k}",
            "bbox": [round(v, 1) for v in line.box],
            "words": len(line.words),
            "strokes": len(line.indices),
            "height_ratio": round((y1 - y0) / a.ref_h, 2) if a.ref_h else 0.0,
            "indent_level": line.level,
            "gap_above": (None if k == 0
                          else round((line.baseline - a.lines[k - 1].baseline) / a.pitch, 2)),
            "starts_with_mark": bool(line.words and _is_mark(line.words[0], a.ref_h)
                                     and len(line.words) >= 2),
            "looks_like_text": line.is_text,
            "nearby": [f"L{j}" for j in (k - 1, k + 1) if 0 <= j < len(a.lines)],
        })
    return out


# --- measuring the result --------------------------------------------------------------
def metrics(boxes, a=None, roles=None):
    """Numbers that should go DOWN when a page gets cleaner.

    baseline_spread  mean distance from a stroke's bottom to its line's baseline
    pitch_spread     mean deviation of a body line gap from the median gap
    margin_spread    mean distance from a line's start to its indent level
    gap_spread       mean deviation of a word gap from the target gap

    Gaps the engine is *supposed* to leave irregular — a section break, the extra room
    around a heading — are excluded. Counting them would score a page as ragged exactly
    because its structure was preserved, which is the opposite of the truth.
    """
    a = a or analyze(boxes)
    if not a.lines:
        return {"baseline_spread": 0.0, "pitch_spread": 0.0,
                "margin_spread": 0.0, "gap_spread": 0.0}
    role = (lambda k: roles[k] if roles and k < len(roles) else PARAGRAPH)

    # Only writing is scored. A drawing is never moved, so counting its rows would report
    # a page as ragged because of ink the engine deliberately refused to touch.
    text = [(k, l) for k, l in enumerate(a.lines) if l.is_text]
    bs = [abs(boxes[i][3] - line.baseline) for _, line in text for i in line.indices
          if boxes[i][3] - boxes[i][1] >= TALL * a.ref_h]
    ps = []
    for group in a.blocks:
        rows = [(k, a.lines[k]) for k in group]
        ps += [abs((b.baseline - t.baseline) - a.pitch)
               for (kt, t), (kb, b) in zip(rows, rows[1:])
               if b.baseline - t.baseline <= PARA_RATIO * a.pitch
               and HEADING not in (role(kt), role(kb))]
    ms = [abs(line.box[0] - line.level_x) for _, line in text]
    gs = [abs((line.words[k + 1].box[0] - line.words[k].box[2]) - a.word_gap)
          for _, line in text for k in range(len(line.words) - 1)]
    mean = lambda v: sum(v) / len(v) if v else 0.0     # noqa: E731
    return {"baseline_spread": mean(bs), "pitch_spread": mean(ps),
            "margin_spread": mean(ms), "gap_spread": mean(gs)}


def moved(boxes, offsets):
    return [(x0 + dx, y0 + dy, x1 + dx, y1 + dy)
            for (x0, y0, x1, y1), (dx, dy) in zip(boxes, offsets)]

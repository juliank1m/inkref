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
from dataclasses import dataclass, field
from statistics import median

# --- tunables, all in multiples of ref_h unless noted ---------------------------------
ROW_BASELINE_TOL = 0.45     # a stroke joins a row if its bottom is this close to it
ROW_OVERLAP = 0.50          # ...or if it vertically overlaps the row this much
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

# Semantic roles. Geometry can guess some of these; a vision model can do better (see
# inkport/ai/). Either way the role only ever selects which deterministic rule applies —
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
    level: int = 0       # indent level, filled in by analyze()

    @property
    def indices(self):
        return [i for w in self.words for i in w.indices]


@dataclass
class Analysis:
    lines: list = field(default_factory=list)
    ref_h: float = 1.0
    pitch: float = 1.0
    levels: list = field(default_factory=list)   # x of each indent level
    word_gap: float = 0.0                        # target gap between words
    n_boxes: int = 0

    @property
    def words(self):
        return [w for line in self.lines for w in line.words]


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

    rows = []
    for i in sorted(tall, key=lambda i: boxes[i][3]):
        x0, y0, x1, y1 = boxes[i]
        best, best_err = None, None
        for row in rows:
            rb = _baseline(row, boxes, ref_h)
            ry0 = min(boxes[j][1] for j in row)
            ry1 = max(boxes[j][3] for j in row)
            overlap = min(y1, ry1) - max(y0, ry0)
            h = min(y1 - y0, ry1 - ry0)
            fits = (abs(y1 - rb) <= ROW_BASELINE_TOL * ref_h
                    or (h > 0 and overlap / h >= ROW_OVERLAP))
            if fits and (best_err is None or abs(y1 - rb) < best_err):
                best, best_err = row, abs(y1 - rb)
        (rows.append([i]) if best is None else best.append(i))

    for i in sorted(short, key=lambda i: boxes[i][3]):
        x0, y0, x1, y1 = boxes[i]
        best, best_score = None, 0.0
        for row in rows:
            ry0 = min(boxes[j][1] for j in row)
            ry1 = max(boxes[j][3] for j in row)
            score = min(y1, ry1) - max(y0, ry0)          # vertical overlap, in points
            if score > best_score:
                best, best_score = row, score
        if best is None:                                 # not inside any row: nearest one
            cy = (y0 + y1) / 2
            near = [(abs(cy - _baseline(r, boxes, ref_h)), r) for r in rows]
            near = [(d, r) for d, r in near if d <= 1.5 * ref_h]
            best = min(near, key=lambda t: t[0])[1] if near else None
        (rows.append([i]) if best is None else best.append(i))
    return rows


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

    a.ref_h = _ref_height(boxes)

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

    diffs = [b.baseline - t.baseline for t, b in zip(a.lines, a.lines[1:])]
    diffs = [d for d in diffs if d > 0]
    a.pitch = median(diffs) if diffs else a.ref_h * 1.6

    a.levels = _levels([l.box[0] for l in a.lines], INDENT_TOL * a.ref_h)
    for line in a.lines:
        line.level = min(range(len(a.levels)),
                         key=lambda k: abs(a.levels[k] - line.box[0]))

    gaps = [line.words[k + 1].box[0] - line.words[k].box[2]
            for line in a.lines for k in range(len(line.words) - 1)]
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


def _line_targets(baselines, pitch, s, role):
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
        if role(k) in FROZEN_ROLES:
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
        if role(k - 1) not in FROZEN_ROLES:
            out[k - 1] = min(out[k - 1], out[k] - MIN_LINE_GAP * pitch)
    for k in range(1, len(out)):                      # never overtake the line above
        if role(k) not in FROZEN_ROLES:
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
            found.setdefault(line.level, []).append(
                line.words[1].box[0] - a.levels[line.level])
    return {lvl: median(v) for lvl, v in found.items()}


def plan(a, s=BALANCED, roles=None):
    """Analysis -> [(dx, dy)] parallel to the original box list.

    All translations, composed per word:
      line spacing   (SPEC §8.6)  vertical, whole line
      baseline align (SPEC §8.4)  vertical, per word within its line
      margin align   (SPEC §8.7)  horizontal, whole line, toward its indent level
      word spacing   (SPEC §8.5)  horizontal, cumulative along the line

    `roles` is one role per line, in `a.lines` order — usually from `inkport.ai`, and
    `None` means treat everything as prose. A role never supplies a coordinate; it only
    chooses which of the rules above apply, which is the whole point of the split.
    """
    s = strength(s)
    offsets = [(0.0, 0.0)] * a.n_boxes
    if not a.lines:
        return offsets
    roles = list(roles) if roles else [PARAGRAPH] * len(a.lines)
    if len(roles) != len(a.lines):
        raise ValueError(f"got {len(roles)} roles for {len(a.lines)} lines")

    targets = _line_targets([l.baseline for l in a.lines], a.pitch, s, lambda k: roles[k])
    bullets = _bullet_offsets(a, roles)
    cap = s.max_shift * a.ref_h
    base_dead, base_gain = s.baseline
    marg_dead, marg_gain = s.margin
    sp_dead, sp_gain = s.spacing

    for line, target, role in zip(a.lines, targets, roles):
        if role in FROZEN_ROLES:
            continue                                   # keeps (0.0, 0.0): never touched
        ldy = target - line.baseline
        level_x = a.levels[line.level]
        ldx = _correct(level_x - line.box[0], marg_dead * a.ref_h, marg_gain)
        listed = (role == BULLET and line.level in bullets and len(line.words) >= 2
                  and _is_mark(line.words[0], a.ref_h))

        shift = 0.0
        prev_right = None
        for wi, w in enumerate(line.words):
            if prev_right is not None:
                if listed and wi == 1:
                    # hang the item text off a shared offset instead of a generic gap,
                    # so a list reads as a column (SPEC §17.2)
                    want = level_x + bullets[line.level]
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


def beautify(boxes, s=BALANCED, roles=None):
    """-> (Analysis, offsets). The whole engine in one call."""
    a = analyze(boxes)
    return a, plan(a, s, roles)


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

    bs = [abs(boxes[i][3] - line.baseline) for line in a.lines for i in line.indices
          if boxes[i][3] - boxes[i][1] >= TALL * a.ref_h]
    ps = [abs((b.baseline - t.baseline) - a.pitch)
          for k, (t, b) in enumerate(zip(a.lines, a.lines[1:]))
          if b.baseline - t.baseline <= PARA_RATIO * a.pitch
          and HEADING not in (role(k), role(k + 1))]
    ms = [abs(line.box[0] - a.levels[line.level]) for line in a.lines]
    gs = [abs((line.words[k + 1].box[0] - line.words[k].box[2]) - a.word_gap)
          for line in a.lines for k in range(len(line.words) - 1)]
    mean = lambda v: sum(v) / len(v) if v else 0.0     # noqa: E731
    return {"baseline_spread": mean(bs), "pitch_spread": mean(ps),
            "margin_spread": mean(ms), "gap_spread": mean(gs)}


def moved(boxes, offsets):
    return [(x0 + dx, y0 + dy, x1 + dx, y1 + dy)
            for (x0, y0, x1, y1), (dx, dy) in zip(boxes, offsets)]

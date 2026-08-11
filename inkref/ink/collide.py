"""Every stroke on the page, whether or not anything claims it.

The layout engine reasons about lines and words. The page does not have lines and words on
it — it has ink, and 10-15% of it is never recognised: a diagram, a margin note, an arrow,
a crossing-out, a symbol the recogniser had no name for. That ink does not move, and it is
invisible to a planner that only knows about the ink it grouped. Spacing lines around it
drives recognised text straight into it.

So the plan is checked against *all* the ink before it is applied:

    candidate transformed bounds
        -> collision against all ink not owned by this moving group
        -> collision against protected regions (an equation, a diagram)
        -> page bounds
        -> safe? apply : reduce, and cancel if it cannot be reduced enough

The test is **new** overlap, not overlap. Letters within a word already overlap, a
descender already reaches into the line below, and a page of dense notes is full of ink
that touches. Vetoing any contact would veto everything. What must never happen is ink
becoming more entangled than the writer left it, so a pair that already overlapped is
allowed to keep overlapping by as much as it did, and no more.

This is the representation Stage 8 (line spacing) needs before it can be switched back on.
It is used now for the corrections that *are* enabled, which is what keeps it honest — an
unused safety net is not a safety net.
"""
from . import layout

# How far a move may be reduced before it is abandoned. A move worth a quarter of its
# intent is still worth making; below that the page is telling us there is no room.
STEPS = (1.0, 0.6, 0.35, 0.0)

# Ink is allowed to come this much closer than it already was, as a share of the writing
# height. Exactly zero would make any rounding a veto.
SLACK = 0.02


class InkMap:
    """Every stroke box on a page, indexed for "what else is near here?".

    A uniform grid, cell about two writing heights across. A page of ten thousand strokes
    is queried once per moving word, and a linear scan per query is ten million box tests —
    which is how a safety check ends up being switched off for being slow.
    """

    def __init__(self, boxes, ref_h=1.0, page=None):
        self.boxes = [tuple(map(float, b)) for b in boxes]
        self.ref_h = max(float(ref_h), 1e-6)
        self.page = page                      # (width, height) in points, or None
        self.cell = max(2.0 * self.ref_h, 1e-6)
        self.grid = {}
        for i, b in enumerate(self.boxes):
            for key in self._cells(b):
                self.grid.setdefault(key, []).append(i)

    def _cells(self, box):
        x0, y0, x1, y1 = box
        for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for cy in range(int(y0 // self.cell), int(y1 // self.cell) + 1):
                yield (cx, cy)

    def near(self, box, pad=0.0):
        """-> indices of strokes whose box may touch `box`, expanded by `pad`."""
        x0, y0, x1, y1 = box
        probe = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        out = set()
        for key in self._cells(probe):
            out.update(self.grid.get(key, ()))
        return out


def _overlap(a, b):
    """Area of intersection. 0 when they do not touch."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _shift(box, dx, dy):
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def constrain(a, boxes, offsets, roles=None, page=None):
    """Reduce any part of a plan that would drive ink into other ink. -> (offsets, report).

    `offsets` is the planner's proposal, one per stroke. What comes back is the same list
    with individual groups scaled down or dropped. Nothing is ever increased, and a group
    that was not moving is never made to move, so this can only make a plan gentler.

    A "group" here is whatever the planner moves as one piece — a word, or a whole rigid
    line. Its strokes are tested together, because a word half-moved is torn.

    `report` counts what happened, which is the difference between "the page had no room"
    and "the planner did nothing".
    """
    boxes = [tuple(map(float, b)) for b in boxes]
    if not a.lines or not any(dx or dy for dx, dy in offsets):
        return offsets, {"groups": 0, "reduced": 0, "cancelled": 0}

    ink = InkMap(boxes, a.ref_h, page)
    role_of = (lambda k: roles[k] if roles and k < len(roles) else layout.PARAGRAPH)
    slack = SLACK * a.ref_h

    # Which line each stroke belongs to, and which strokes are protected. Ink inside one
    # line is allowed to stay entangled with itself — that is what a line is.
    line_of = {}
    protected = set()
    for k, line in enumerate(a.lines):
        frozen = role_of(k) in layout.FROZEN_ROLES
        for i in line.indices:
            line_of[i] = k
            if frozen:
                protected.add(i)

    out = list(offsets)
    report = {"groups": 0, "reduced": 0, "cancelled": 0}
    for k, line in enumerate(a.lines):
        for word in line.words:
            idx = [i for i in word.indices if i < len(out)]
            moving = [out[i] for i in idx]
            if not idx or not any(dx or dy for dx, dy in moving):
                continue
            report["groups"] += 1
            # Every stroke of a word carries the same offset in every current transform;
            # taking the largest keeps this honest if that ever stops being true.
            dx = max((o[0] for o in moving), key=abs)
            dy = max((o[1] for o in moving), key=abs)

            best = 0.0
            for scale in STEPS:
                if scale == 0.0:
                    break
                if _fits(ink, idx, boxes, out, line_of, protected, k,
                         dx * scale, dy * scale, slack, page):
                    best = scale
                    break
            if best < 1.0:
                report["cancelled" if best == 0.0 else "reduced"] += 1
                for i in idx:
                    out[i] = (out[i][0] * best, out[i][1] * best)
    return out, report


def _fits(ink, idx, boxes, offsets, line_of, protected, line_k, dx, dy, slack, page):
    """True if moving `idx` by (dx, dy) creates no new entanglement and stays on the page."""
    own = set(idx)
    reach = max(abs(dx), abs(dy)) + ink.cell
    for i in idx:
        before = boxes[i]
        after = _shift(before, dx, dy)
        if page and (after[0] < -slack or after[1] < -slack
                     or after[2] > page[0] + slack or after[3] > page[1] + slack):
            return False
        for j in ink.near(after, reach):
            if j in own or j >= len(offsets):
                continue
            # Ink inside the same line is allowed to keep touching itself.
            if line_of.get(j) == line_k and j not in protected:
                continue
            # Judged where the other stroke ENDS UP, not where it started, so the answer
            # does not depend on which group is considered first.
            other_now = _shift(boxes[j], offsets[j][0], offsets[j][1])
            was = _overlap(before, boxes[j])
            now = _overlap(after, other_now)
            if now > was + slack * slack:
                return False
            # Protected ink is stricter: an equation or a diagram may not be approached at
            # all, not merely not overlapped further.
            if j in protected and now > 0 and was == 0:
                return False
    return True

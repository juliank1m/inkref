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

# Unowned ink this close to a moving group counts as tethered to it, and may not be left
# more than SEPARATION further behind. Both a share of the writing height. Deliberately
# looser than the follower rule (ink/flow.py): a stroke too ambiguous to travel with a line
# is exactly the stroke that must not be abandoned by it.
TETHER = 1.20
SEPARATION = 0.50


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


def _gap(a, b):
    """Shortest distance between two boxes. 0 when they touch or overlap."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _line_dy(line, offsets):
    """The line's own vertical shift: the median of its strokes', so one stray word does
    not speak for the line."""
    dys = sorted(offsets[i][1] for i in line.indices if i < len(offsets))
    return dys[len(dys) // 2] if dys else 0.0


def _shift(box, dx, dy):
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def ownership(a, followers=None):
    """-> (stroke index -> owning line index). Followers are owned by the line they follow.

    Ownership is what "may keep touching" means. Ink inside one line is allowed to stay
    entangled with itself, and a dot on an `i` that travels with its line is part of that
    line for this purpose even though no recognised word claimed it.
    """
    owner = {}
    for k, line in enumerate(a.lines):
        for i in line.indices:
            owner[i] = k
    for i, k in (followers or {}).items():
        owner.setdefault(i, k)
    return owner


def constrain(a, boxes, offsets, roles=None, page=None, groups=None, followers=None):
    """Reduce any part of a plan that would drive ink into other ink. -> (offsets, report).

    `offsets` is the planner's proposal, one per stroke. What comes back is the same list
    with individual groups scaled down or dropped. Nothing is ever increased, and a group
    that was not moving is never made to move, so this can only make a plan gentler.

    A "group" here is whatever the planner moves as one piece — a word, a whole rigid line,
    or (in Stage 8) a line together with the unread ink that follows it. Its strokes are
    tested together, because a word half-moved is torn. `groups` overrides the default of
    one group per word; `followers` extends ownership so a line's dots and punctuation are
    not treated as obstacles by the very line they belong to.

    `report` counts what happened, which is the difference between "the page had no room"
    and "the planner did nothing".
    """
    boxes = [tuple(map(float, b)) for b in boxes]
    if not a.lines or not any(dx or dy for dx, dy in offsets):
        return offsets, {"groups": 0, "reduced": 0, "cancelled": 0}

    ink = InkMap(boxes, a.ref_h, page)
    slack = SLACK * a.ref_h
    owner = ownership(a, followers)
    protected = protected_strokes(a, roles, followers)

    if groups is None:
        groups = [(word.indices, k) for k, line in enumerate(a.lines)
                  for word in line.words]

    out = list(offsets)
    report = {"groups": 0, "reduced": 0, "cancelled": 0}
    # Two rounds. The ordering repair below reduces offsets that the gate had already
    # approved at their full size, and a reduced move lands somewhere nobody validated —
    # which put 26 new overlaps on a real page. Re-gating what ordering changed closes that,
    # and since both passes only ever reduce, the pair converges rather than oscillating.
    for round_ in range(GATE_ROUNDS):
        out = _gate(a, boxes, out, ink, owner, protected, groups, slack, page,
                    report if round_ == 0 else None)
        out, crossed = _uncross(a, boxes, out, groups)
        report["uncrossed"] = report.get("uncrossed", 0) + crossed
        if not crossed:
            break
    return out, report


# Gate and ordering repair alternate this many times at most. Both only reduce, so the
# sequence is monotone and terminates; two rounds settles every page measured.
GATE_ROUNDS = 2


def _gate(a, boxes, offsets, ink, owner, protected, groups, slack, page, report):
    out = list(offsets)
    for indices, k in groups:
        idx = [i for i in indices if i < len(out)]
        moving = [out[i] for i in idx]
        if not idx or not any(dx or dy for dx, dy in moving):
            continue
        if report is not None:
            report["groups"] += 1
        # Every stroke of a group carries the same offset in every current transform;
        # taking the largest keeps this honest if that ever stops being true.
        dx = max((o[0] for o in moving), key=abs)
        dy = max((o[1] for o in moving), key=abs)

        best = 0.0
        for scale in STEPS:
            if scale == 0.0:
                break
            if _fits(ink, idx, boxes, out, owner, protected, k,
                     dx * scale, dy * scale, slack, page):
                best = scale
                break
        if best < 1.0:
            if report is not None:
                report["cancelled" if best == 0.0 else "reduced"] += 1
            for i in idx:
                out[i] = (out[i][0] * best, out[i][1] * best)
    return out


# Passes of the ordering fix. It only ever halves, so it converges: at zero the layout is
# the original one, which is ordered by construction. Four passes is far past what a real
# page needs and still bounded.
ORDER_PASSES = 4


def order(a, boxes, offsets, report=None, groups=None):
    """Undo any reading-order inversion the gate created. -> offsets, reduced only.

    Word spacing is *cumulative along a line*: each word's shift assumes the ones before it
    moved too. The gate judges each word on its own, so holding one back while its
    neighbour goes can slide them past each other — a `=` ending up right of the `0` it
    preceded. Measured once on a real page: one inversion in about fifteen hundred words.
    Rare, and unmistakable when it happens.

    Repaired by halving the larger of the two offending offsets until the pair is ordered
    again. Only ever reducing keeps the gate's guarantee intact — nothing here can put back
    a move the gate refused, and at zero the words sit where the writer left them.
    """
    out, fixed = _uncross(a, boxes, offsets, groups)
    if report is not None and fixed:
        report["uncrossed"] = fixed
    return out


def _uncross(a, boxes, offsets, groups=None):
    """-> (offsets, how many crossings were repaired).

    `groups` names what moves as one piece. Without it a repair halves one stroke of a word
    and leaves the rest, which un-crosses the pair by tearing the word in two — the precise
    thing every other rule here exists to prevent.
    """
    out = list(offsets)
    fixed = 0
    members = {}
    for indices, _ in (groups or []):
        for i in indices:
            members[i] = indices

    def ease(i):
        for j in members.get(i, [i]):
            if j < len(out):
                out[j] = (out[j][0] / 2, out[j][1])

    def ease_dy(indices):
        for j in indices:
            if j < len(out):
                out[j] = (out[j][0], out[j][1] / 2)
    for _ in range(ORDER_PASSES):
        crossed = 0
        for line in a.lines:
            words = [w for w in line.words if w.indices]
            for u, v in zip(words, words[1:]):
                i, j = u.indices[0], v.indices[0]
                if i >= len(out) or j >= len(out):
                    continue
                if boxes[j][0] + out[j][0] >= boxes[i][0] + out[i][0]:
                    continue
                crossed += 1
                # halve whichever is straying further; if both are still, they cannot cross
                k = i if abs(out[i][0]) >= abs(out[j][0]) else j
                if out[k][0] == 0.0:
                    k = j if k == i else i
                ease(k)

        # ...and the same hazard one level up. Baseline alignment gives each word its own
        # dy, the gate reduces them one at a time, and two lines of a column can end up
        # swapped. Halving every stroke of the straying line keeps the line intact while
        # it retreats.
        for group in (a.blocks or []):
            rows = sorted(group, key=lambda k: a.lines[k].baseline)
            for p, q in zip(rows, rows[1:]):
                top = a.lines[p].baseline + _line_dy(a.lines[p], out)
                bot = a.lines[q].baseline + _line_dy(a.lines[q], out)
                if bot >= top:
                    continue
                crossed += 1
                straying = p if abs(_line_dy(a.lines[p], out)) >= abs(
                    _line_dy(a.lines[q], out)) else q
                ease_dy([i for grp, k2 in (groups or [])
                         if k2 == straying for i in grp]
                        or a.lines[straying].indices)
        fixed += crossed
        if not crossed:
            break
    return out, fixed


def protected_strokes(a, roles=None, followers=None):
    """Strokes belonging to a region that may not be approached at all."""
    role_of = (lambda k: roles[k] if roles and k < len(roles) else layout.PARAGRAPH)
    out = set()
    for k, line in enumerate(a.lines):
        if role_of(k) in layout.FROZEN_ROLES:
            out.update(line.indices)
    for i, k in (followers or {}).items():
        if role_of(k) in layout.FROZEN_ROLES:
            out.add(i)
    return out


def fits(a, boxes, offsets, indices, group, dx, dy, roles=None, page=None,
         followers=None, ink=None):
    """Would moving `indices` (which belong to line `group`) by (dx, dy) be safe?

    The same predicate `constrain` uses, exposed so a planner can ask before committing —
    Stage 8 proposes a whole block's worth of movement and needs to know whether it holds
    before it decides how much of it to keep.
    """
    boxes = [tuple(map(float, b)) for b in boxes]
    return _fits(ink or InkMap(boxes, a.ref_h, page), list(indices), boxes, offsets,
                 ownership(a, followers), protected_strokes(a, roles, followers),
                 group, dx, dy, SLACK * a.ref_h, page)


def _fits(ink, idx, boxes, offsets, owner, protected, line_k, dx, dy, slack, page):
    """True if moving `idx` by (dx, dy) is safe: no new entanglement, nothing abandoned,
    and still on the page."""
    own = set(idx)
    reach = max(abs(dx), abs(dy)) + ink.cell
    tether = TETHER * ink.ref_h
    limit = SEPARATION * ink.ref_h
    for i in idx:
        before = boxes[i]
        after = _shift(before, dx, dy)
        if page and (after[0] < -slack or after[1] < -slack
                     or after[2] > page[0] + slack or after[3] > page[1] + slack):
            return False
        for j in ink.near(after, reach):
            if j in own or j >= len(offsets):
                continue
            # Ink owned by the same line is allowed to keep touching itself — including
            # the followers that travel with it.
            if owner.get(j) == line_k and j not in protected:
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

        # The other danger, and the one collision cannot see: moving AWAY from ink that was
        # part of us. A comma the recogniser missed and the follower rule would not commit
        # to is unowned and stationary; slide its line out from under it and the page tears
        # even though nothing collided. So ink that started tethered to this group must not
        # be stranded — if it would be, the move is refused and something gentler is tried.
        for j in ink.near(before, tether):
            if j in own or j >= len(offsets) or owner.get(j) is not None:
                continue        # owned ink travels with its own group; only strays matter
            if offsets[j][0] or offsets[j][1]:
                continue        # already moving under some other group's plan
            near_before = _gap(before, boxes[j])
            if near_before > tether:
                continue
            if _gap(after, boxes[j]) > max(near_before, 0.0) + limit:
                return False
    return True

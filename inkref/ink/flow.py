"""Line spacing: the one correction that moves whole lines past each other.

It was switched off for a release because it is the only transform that can tear a page.
Every other correction moves ink *within* a line, so the worst it does is look odd. This one
slides lines through the space between them, and that space is not empty: 10% of a real page
is ink no recogniser claimed, and it does not move.

Four ideas make it safe enough to switch back on, in the order they run:

    followers    unread ink that clearly belongs to one line travels with it
    blocks       lines are spaced within a flow, never across a column or an equation
    targets      the desired rhythm comes from the page's own measured pitch
    acceptance   a whole block's proposed layout is gated before any of it is applied

The last is the important one. Spacing is not "move each line toward its nearest target" —
lines are not independent, and a plan that is safe line by line can still be nonsense as a
whole. A block is planned complete, tested complete, and reduced or abandoned complete.

Nothing here decides a number from semantics. A role says a heading wants more room around
it; how much room, and whether there is any, is geometry's answer (SPEC §13).
"""
from statistics import median

from . import collide
from . import layout

# --- Stage 8A: which unread strokes belong to a line ------------------------------------
#
# Measured on a real page: of 1082 unclaimed strokes, the median is 0.28 of the writing
# height — dots, commas, accents, fragments of a letter the recogniser split. They are
# nearly all *small* and *close*. But a tenth of them sit almost equidistant between two
# lines (second-nearest only 1.11x further), and those are exactly the ones a confident
# rule would get wrong. Hence a margin test rather than a distance test alone.
FOLLOW_NEAR = 0.60        # x ref_h: no further than this from the line's own ink
FOLLOW_MARGIN = 1.60      # ...and the next-nearest line must be this much further again
FOLLOW_MAX_SIZE = 0.90    # x ref_h: bigger than this is structure, not a fragment
FOLLOW_SIDE = 0.50        # x ref_h: horizontal slop on the line's extent
# ...and no further than this from the line's BASELINE. The distance above is measured to
# the line's box, which on mathematical writing is tall — a row carrying an exponent and a
# subscript spans three writing heights, so ink 0.6 from its box can be 2.8 from the line
# it would travel with. Measured on a real page before this existed: half the followers sat
# more than a writing height off the baseline, and the worst was nearly a whole pitch away.
# An ascender or an accent reaches about 1.2; beyond that it is somebody else's ink.
FOLLOW_BAND = 1.60

# --- Stage 8B/8C: flow blocks and their rhythm ------------------------------------------
BLOCK_BREAK = 1.70        # x pitch: a gap wider than this is a deliberate separation
MIN_BLOCK = 3             # fewer lines than this has no rhythm worth normalising
HEADING_LEAD = 1.35       # x pitch: room a heading wants above it
HEADING_TRAIL = 1.15      # ...and below it
MAX_BLOCK_SHIFT = 3.0     # x ref_h: cap on any one line's spacing move


def followers(a, boxes, unmatched, roles=None):
    """-> {stroke index: line index} for unread ink that clearly belongs to one line.

    A stroke follows AT MOST one line, and only when the answer is not close. Everything
    else stays an obstacle, which is the conservative half of the invariant:

        if unread ink clearly belongs to a moving line, it travels with it;
        if we are not sure, nothing moves it.

    Purely geometric on purpose. The recogniser has already had its say — these are the
    strokes it did not claim — so asking it again would just be asking the same question of
    the same evidence.
    """
    if not a.lines or not unmatched:
        return {}
    role_of = (lambda k: roles[k] if roles and k < len(roles) else layout.PARAGRAPH)
    near = FOLLOW_NEAR * a.ref_h
    side = FOLLOW_SIDE * a.ref_h
    biggest = FOLLOW_MAX_SIZE * a.ref_h
    band = FOLLOW_BAND * a.ref_h

    # Only lines that could move are worth following, and a frozen region never adopts:
    # an exponent belonging to an equation must not be handed to the prose line beside it.
    envelopes = [(k, line) for k, line in enumerate(a.lines)
                 if line.is_text and role_of(k) not in layout.FROZEN_ROLES]
    if not envelopes:
        return {}

    ink = collide.InkMap([tuple(l.box) for _, l in envelopes], a.ref_h)
    owned = collide.ownership(a)
    out = {}
    for i in unmatched:
        b = boxes[i]
        if max(b[2] - b[0], b[3] - b[1]) > biggest:
            continue                                  # structure, not a fragment
        scored = []
        for slot in ink.near(b, near + side):
            k, line = envelopes[slot]
            cx = (b[0] + b[2]) / 2
            if not (line.box[0] - side <= cx <= line.box[2] + side):
                continue                              # not under this line at all
            if abs((b[1] + b[3]) / 2 - line.baseline) > band:
                continue                              # too far off its baseline to be its ink
            d = collide._gap(b, line.box)
            if d <= near:
                scored.append((d, k))
        if not scored:
            continue
        scored.sort()
        best_d, best_k = scored[0]
        # Ambiguous: two lines have an equal claim, so neither gets it.
        if len(scored) > 1 and scored[1][0] < FOLLOW_MARGIN * max(best_d, 1e-6):
            continue
        # Would attaching it bridge two lines? A stroke long enough to reach into the
        # next line's ink is a connector, a bracket or a crossing-out, not a dot.
        if _bridges(b, boxes, owned, best_k, a.ref_h):
            continue
        out[i] = best_k
    return out


def word_groups(a, follow, boxes):
    """-> [(stroke indices, line index)], one per word, with its followers folded in.

    The gate moves a group as one piece, so a comma only travels with its word if it is IN
    that word's group. Left out, the word slides up to a character's width sideways under
    the word-spacing correction and its punctuation stays where it was — measured at 6.2pt
    on a real page, which is a comma stranded a whole letter away from the word it ends.

    Followers join the word they sit over, by horizontal nearness. The vertical question was
    already settled when the follower was assigned to this line.
    """
    extra = {}
    for i, k in follow.items():
        words = a.lines[k].words if k < len(a.lines) else []
        if not words:
            continue
        cx = (boxes[i][0] + boxes[i][2]) / 2
        w = min(range(len(words)),
                key=lambda n: max(words[n].box[0] - cx, cx - words[n].box[2], 0.0))
        extra.setdefault((k, w), []).append(i)
    return [(list(word.indices) + extra.get((k, n), []), k)
            for k, line in enumerate(a.lines) for n, word in enumerate(line.words)]


def adopt(groups, offsets, follow):
    """Give every follower the offset already planned for the word it joined.

    Without this the group is torn before the gate ever sees it: the planner has no idea
    the follower exists, so it leaves it at zero while its word carries a real shift, and a
    gate that only ever scales a group *uniformly* preserves that difference exactly. The
    comma then stays put while its word slides away — 10.8pt of it on a real page.

    Seeding here rather than after gating is what makes the move honest: the gate then
    validates the word and its punctuation as the single object they are.
    """
    out = list(offsets)
    for idx, _ in groups:
        owned = [i for i in idx if i not in follow and i < len(out)]
        if not owned:
            continue
        dx, dy = out[owned[0]]
        for i in idx:
            if i in follow and i < len(out):
                out[i] = (dx, dy)
    return out


def _bridges(box, boxes, owned, line_k, ref_h):
    """True if this stroke also reaches ink belonging to a *different* line."""
    grown = (box[0] - 0.1 * ref_h, box[1] - 0.1 * ref_h,
             box[2] + 0.1 * ref_h, box[3] + 0.1 * ref_h)
    for j, other in enumerate(boxes):
        k = owned.get(j)
        if k is None or k == line_k:
            continue
        if collide._overlap(grown, other) > 0:
            return True
    return False


def blocks(a, roles=None):
    """-> [[line index]] runs of lines that share one vertical flow.

    A block never spans a column, never crosses an equation or a diagram, and never
    swallows a deliberate gap. Spacing inside one says nothing about any other, which is
    what stops a tight column being "fixed" using a loose column's rhythm.
    """
    role_of = (lambda k: roles[k] if roles and k < len(roles) else layout.PARAGRAPH)
    out = []
    for column in (a.blocks or [list(range(len(a.lines)))]):
        rows = sorted((k for k in column if a.lines[k].is_text),
                      key=lambda k: a.lines[k].baseline)
        run = []
        for k in rows:
            frozen = role_of(k) in layout.FROZEN_ROLES
            gap = (a.lines[k].baseline - a.lines[run[-1]].baseline) if run else 0.0
            broken = frozen or (run and gap > BLOCK_BREAK * a.pitch)
            if broken:
                if len(run) >= MIN_BLOCK:
                    out.append(run)
                run = []
            if frozen:
                continue        # a protected region is a boundary, never a member
            run.append(k)
        if len(run) >= MIN_BLOCK:
            out.append(run)
    return out


def targets(a, block, roles=None):
    """-> desired baseline per line of `block`, anchored on its first line.

    The rhythm is the block's own: the median gap between its comparable prose lines, not
    a constant and not the page average. A heading asks for more room around it, and that
    is the only thing a role contributes — the amount is still measured, never told.
    """
    role_of = (lambda k: roles[k] if roles and k < len(roles) else layout.PARAGRAPH)
    lines = [a.lines[k] for k in block]
    gaps = [b.baseline - t.baseline for t, b in zip(lines, lines[1:])
            if b.baseline - t.baseline > 0]
    if not gaps:
        return [l.baseline for l in lines]
    # Prose-to-prose gaps only where there are enough of them: a block that is mostly
    # headings should not take its rhythm from the space around them.
    plain = [g for g, (t, b) in zip(gaps, zip(block, block[1:]))
             if role_of(t) != layout.HEADING and role_of(b) != layout.HEADING]
    pitch = median(plain if len(plain) >= 2 else gaps)

    out = [lines[0].baseline]
    for prev, cur in zip(block, block[1:]):
        want = pitch
        if role_of(prev) == layout.HEADING:
            want = HEADING_TRAIL * pitch
        elif role_of(cur) == layout.HEADING:
            want = HEADING_LEAD * pitch
        out.append(out[-1] + want)
    return out


def space(a, boxes, offsets, roles=None, unmatched=(), page=None, s=layout.BALANCED,
          follow=None):
    """Stage 8. -> (offsets, report). Adds one shared dy per line, on top of `offsets`.

    The move for a line covers everything that line owns: the strokes its words claimed
    *and* the followers assigned to it, all with the same dy. Words are never repositioned
    against each other here — the within-line corrections already did that, and this stage
    moves the finished line as one piece.
    """
    report = {"blocks": 0, "moved": 0, "reduced": 0, "dropped": 0,
              "followers": 0, "lines": 0}
    if not a.lines or a.pitch <= 0:
        return offsets, report

    follow = followers(a, boxes, unmatched, roles) if follow is None else follow
    report["followers"] = len(follow)
    by_line = {}
    for i, k in follow.items():
        by_line.setdefault(k, []).append(i)

    out = list(offsets)
    ink = collide.InkMap([tuple(map(float, b)) for b in boxes], a.ref_h, page)
    cap = min(MAX_BLOCK_SHIFT * a.ref_h, s.max_shift * a.ref_h)

    for block in blocks(a, roles):
        report["blocks"] += 1
        want = targets(a, block, roles)
        # The deadband is the same idea as every other correction: a rhythm that is already
        # near enough is left alone, and only the excess is taken out.
        moves = []
        for k, target in zip(block, want):
            err = target - a.lines[k].baseline
            dy = layout._correct(err, s.line[0] * a.pitch, s.line[1])
            moves.append(max(-cap, min(cap, dy)))
        if not any(abs(d) > 1e-6 for d in moves):
            continue

        groups = [([i for i in a.lines[k].indices] + by_line.get(k, []), k)
                  for k in block]
        scale, keep = _accept(a, boxes, out, block, groups, moves, roles, page, ink,
                              follow)
        if keep == 0:
            report["dropped"] += 1
            continue
        if scale < 1.0 or keep < len(block):
            report["reduced"] += 1
        else:
            report["moved"] += 1
        for n in range(keep):
            dy = moves[n] * scale
            if abs(dy) < 1e-9:
                continue
            report["lines"] += 1
            for i in groups[n][0]:
                if i < len(out):
                    out[i] = (out[i][0], out[i][1] + dy)
    # Scaling a whole block cannot invert it, but truncating one can at the junction, and
    # collision alone does not notice two lines that swapped without touching.
    return collide.order(a, boxes, out, groups=word_groups(a, follow, boxes)), report


def _accept(a, boxes, offsets, block, groups, moves, roles, page, ink, follow):
    """-> (scale, lines kept) — the most of this block's plan that is safe.

    Two knobs, and the order matters. Scaling the *whole* block is tried first because
    interpolating between two ordered layouts stays ordered: no amount of uniform easing
    can make line 5 cross line 4. Only when no scale works is the block truncated, keeping
    a prefix and leaving the rest where it is — which is safe for the same reason, since
    the prefix keeps its order and the untouched suffix keeps its own.

    Never the other way round: trimming first would routinely abandon a whole block over
    one tight line at the bottom of it.
    """
    def safe(scale, keep):
        trial = list(offsets)
        for n in range(keep):
            dy = moves[n] * scale
            for i in groups[n][0]:
                if i < len(trial):
                    trial[i] = (trial[i][0], trial[i][1] + dy)
        for n in range(keep):
            dy = moves[n] * scale
            if abs(dy) < 1e-9:
                continue
            idx, k = groups[n]
            # Tested against the trial offsets, so every other line of this block is
            # judged where it ENDS UP rather than where it started.
            if not collide.fits(a, boxes, trial, idx, k, 0.0, dy, roles, page,
                                followers=follow, ink=ink):
                return False
        return True

    for scale in collide.STEPS:
        if scale == 0.0:
            break
        if safe(scale, len(block)):
            return scale, len(block)

    # Nothing fits whole. Binary-search the longest prefix that does, at full strength —
    # a shorter move done properly beats a longer one watered down.
    lo, hi = 0, len(block) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if safe(1.0, mid):
            lo = mid
        else:
            hi = mid - 1
    return 1.0, lo

"""Stage 8 has to actually even out a ragged page, and never tear one doing it.

`tests/test_collide.py` proves the safety gate never makes a page worse. Nothing there
proves the spacing stage does anything at all, and a pass that quietly abandons every block
would satisfy every safety check ever written. Both halves are the same file's business,
because they fail in opposite directions and a fix for one is the classic cause of the
other.

The class of bug caught here:

    a stage that reports work and changes nothing, or evens the rhythm by sliding a line
    past its neighbour;
    punctuation the recogniser never claimed left behind by the line it belongs to, or
    guessed onto the wrong one;
    a rhythm measured across a gutter, an equation or a diagram, so one region is
    "corrected" using another's pitch;
    a role that stops buying a heading its room, silently, on a page where every other
    check still passes;
    a plan approved line by line against the page as it stands, which is safe in every part
    and nonsense as a whole.

Run: /Users/julian/projects/inkref/venv/bin/python tests/test_flow.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.ink import collide, flow, layout      # noqa: E402


def page(rows, ys=None, x0=10.0, height=8.0, pitch=20.0, width=30.0, gap=6.0, into=None):
    """A tidy grid of word boxes -> (boxes, Analysis). `rows` is words per row.

    `ys` overrides the row tops, which is how a ragged page is built: an evenly spaced
    ladder has no error to correct and the stage is right to leave it alone.

    `into` is an existing (boxes, lines) pair to extend, which is how a second column joins
    the same page. Stroke indices are positions in one box list, so two columns cannot be
    built separately and glued together afterwards.
    """
    boxes, lines = into if into else ([], [])
    for r, count in enumerate(rows):
        words = []
        y = 100.0 + r * pitch if ys is None else ys[r]
        for c in range(count):
            x = x0 + c * (width + gap)
            boxes.append((x, y, x + width, y + height))
            words.append(layout.Word(indices=[len(boxes) - 1],
                                     box=boxes[-1], baseline=y + height))
        lines.append(layout.Line(words=words,
                                 box=(words[0].box[0], y, words[-1].box[2], y + height),
                                 baseline=y + height))
    a = layout.Analysis(n_boxes=len(boxes), ref_h=height, pitch=pitch, lines=lines)
    return boxes, layout.statistics(a, boxes)


def maths_page():
    """Prose, a fraction, more prose -> (boxes, Analysis, roles).

    The fraction is one line of four strokes at four different heights: numerator, bar,
    denominator, and an exponent up and to the right. Any re-spacing inside it shows up as
    those four drifting apart. The prose rows are deliberately uneven so that Stage 8 has
    something it wants to correct, and the row above the fraction is close enough that
    taking the correction would drive it into the exponent.
    """
    boxes, a = page([2] * 8,
                    ys=[100.0, 120.0, 150.0, 180.0, 240.0, 270.0, 290.0, 310.0])
    first = len(boxes)
    boxes += [(20.0, 192.0, 40.0, 200.0),      # numerator
              (18.0, 202.0, 44.0, 203.0),      # the bar
              (20.0, 205.0, 40.0, 213.0),      # denominator
              (46.0, 190.0, 52.0, 196.0)]      # exponent
    word = layout.Word(indices=list(range(first, len(boxes))),
                       box=(18.0, 190.0, 52.0, 213.0), baseline=213.0)
    a.lines.append(layout.Line(words=[word], box=word.box, baseline=213.0, rigid=True))
    a.n_boxes = len(boxes)
    roles = [layout.PARAGRAPH] * 8 + [layout.EQUATION]
    return boxes, layout.statistics(a, boxes), roles


def stroke(boxes, a, line_k, word_k, box):
    """Give one word an extra stroke -> its index.

    Real ink reaches outside the band its line's baseline suggests, and that ink is what two
    closing lines meet in.
    """
    boxes.append(tuple(box))
    i = len(boxes) - 1
    word = a.lines[line_k].words[word_k]
    word.indices.append(i)
    word.box = layout._union([word.box, box])
    a.lines[line_k].box = layout._union([a.lines[line_k].box, box])
    a.n_boxes = len(boxes)
    return i


def _gaps(a):
    return [b.baseline - t.baseline for t, b in zip(a.lines, a.lines[1:])]


def _baselines(a, offsets):
    """Where each line ends up. Stage 8 gives one shared dy to a whole line."""
    return [line.baseline + offsets[line.indices[0]][1] for line in a.lines]


def _entanglement(boxes):
    """-> {(i, j): overlap area} for every pair that touches at all."""
    return {(i, j): collide._overlap(boxes[i], boxes[j])
            for i in range(len(boxes)) for j in range(i + 1, len(boxes))
            if collide._overlap(boxes[i], boxes[j]) > 0}


def test_ragged_prose_gets_a_more_even_rhythm():
    """The failure this exists for: the stage runs, reports work, and changes nothing —
    or evens the spacing by sliding a line past its neighbour.

    Three prose lines, 30pt apart then 20pt apart. There is exactly one rhythm those two
    gaps can agree on, so the spread between them must shrink. Reading order is checked on
    the moved boxes rather than on the plan, because an inversion is a property of where
    the ink ends up, not of the offsets that put it there.
    """
    boxes, a = page([2, 2, 2], ys=[100.0, 130.0, 150.0])
    before = _gaps(a)
    assert before == [30.0, 20.0], before

    out, report = flow.space(a, boxes, [(0.0, 0.0)] * len(boxes))
    after = _gaps(layout.reproject(a, layout.moved(boxes, out)))

    spread = lambda g: max(g) - min(g)      # noqa: E731
    assert spread(after) < spread(before), (before, after)
    assert report["lines"] > 0, report      # a dropped block would also leave order intact

    assert all(g > 0 for g in after), f"lines must not cross: {after}"
    shifted = layout.moved(boxes, out)
    for line in a.lines:
        xs = [shifted[w.indices[0]][0] for w in line.words]
        assert xs == sorted(xs), f"words within a line must keep their order: {xs}"
    print("  rhythm: uneven prose gaps converge and no line or word overtakes another")


def test_a_comma_travels_with_the_line_it_hangs_off():
    """The failure this exists for: the line is respaced and its punctuation stays put.

    A comma is exactly the ink no recogniser groups — too small to be a word, too close to
    the line to be an obstacle — so if the line moves without it the page tears in the one
    way collision checking cannot see, because nothing overlapped. The row gaps are
    deliberately uneven so Stage 8 has real work to do; on an even page the deadband is
    never exceeded and the whole check would pass without moving anything.
    """
    boxes, a = page([2] * 5, ys=[100.0, 120.0, 154.0, 174.0, 194.0])
    comma = len(boxes)
    boxes = boxes + [(76.5, 158.0, 79.0, 162.0)]    # a hair right of row 2's last word
    a.n_boxes = len(boxes)

    assert flow.followers(a, boxes, [comma]) == {comma: 2}, flow.followers(
        a, boxes, [comma])

    offsets = [(0.0, 0.0)] * len(boxes)
    out, report = flow.space(a, boxes, offsets, unmatched=[comma])

    assert report["followers"] == 1, report
    # Without a real move the equality below would hold trivially at (0.0, 0.0).
    assert out[5][1] != 0.0, ("the fixture must actually respace row 2", out[5], report)
    assert out[comma] == out[5], (out[comma], out[5])
    assert out[comma][1] == out[4][1], (out[comma], out[4])
    print("  punctuation: an unclaimed comma is adopted and shifts exactly with its line")


def test_a_stroke_between_two_lines_is_neither_adopted_nor_stranded():
    """The failure this exists for: a stray with an equal claim from two lines is left
    unowned, and the line below it is then spaced away as if the page were empty there.

    Row 3 sits too close to row 2, so it and everything under it want to move down. The
    stray sits in that narrowed gap, near enough to either line to be its comma and no
    nearer to one than the other. Adopting it would be a guess and leaving it would tear
    the page, so the only right answer is to move the line less.
    """
    boxes, a = page([2] * 5, ys=[100.0, 130.0, 146.0, 176.0, 206.0])
    stray = len(boxes)
    boxes.append((20.0, 140.4, 23.0, 143.4))    # 2.4 under one line, 2.6 over the next
    a.n_boxes = len(boxes)
    offsets = [(0.0, 0.0)] * len(boxes)

    # Each line on its own would take it, which is what makes the refusal below mean
    # "ambiguous" rather than "out of reach". Freezing a role is how a line is taken out of
    # the running without moving any ink.
    prose = [layout.PARAGRAPH] * len(a.lines)
    hide = lambda k: [layout.EQUATION if j == k else r          # noqa: E731
                      for j, r in enumerate(prose)]
    assert flow.followers(a, boxes, [stray], hide(2)) == {stray: 1}
    assert flow.followers(a, boxes, [stray], hide(1)) == {stray: 2}
    assert flow.followers(a, boxes, [stray]) == {}, "an equal claim is not a claim"

    s = layout.BALANCED
    want = flow.targets(a, flow.blocks(a)[0])
    full = layout._correct(want[2] - a.lines[2].baseline, s.line[0] * a.pitch, s.line[1])
    assert full > collide.SEPARATION * a.ref_h, f"fixture strands nothing: {full}"

    # The gate asked directly, then asked again with the stray deleted: the only thing
    # standing between that line and its full move is the ink it would leave behind.
    line2 = a.lines[2].indices
    assert not collide.fits(a, boxes, offsets, line2, 2, 0.0, full)
    assert collide.fits(a, boxes[:stray], offsets[:stray], line2, 2, 0.0, full)

    out, report = flow.space(a, boxes, offsets, unmatched=[stray], s=s)
    assert out[stray] == (0.0, 0.0), f"an unadopted stray must never move: {out[stray]}"
    assert report["followers"] == 0, report
    assert report["reduced"] == 1 and report["moved"] == 0, report

    applied = out[line2[0]][1]
    assert 0.0 < applied < full, (applied, full)
    before = min(collide._gap(boxes[i], boxes[stray]) for i in line2)
    after = min(collide._gap(collide._shift(boxes[i], 0.0, applied), boxes[stray])
                for i in line2)
    assert after - before <= collide.SEPARATION * a.ref_h + 1e-9, (before, after)
    print("  ambiguous: a stray owned by neither line still holds that line back")


def test_a_heading_is_given_more_room_below_it():
    """The failure this exists for: role=HEADING stops opening the gap under a heading.

    An evenly ruled page is the fixture precisely because it has no spacing error of its
    own. All-prose it is already correct and must be left exactly as written, so any gap
    that grows can only have come from the role. The same page with line 1 marked as a
    heading must push the prose below it further down than one plain pitch, and the run of
    prose underneath must keep its original rhythm — a heading buys room around itself, it
    does not restretch the paragraph.
    """
    boxes, a = page([2] * 5)
    zero = [(0.0, 0.0)] * len(boxes)

    plain, plain_report = flow.space(a, boxes, zero, roles=[layout.PARAGRAPH] * 5)
    flat = _baselines(a, plain)
    assert flat == [108.0, 128.0, 148.0, 168.0, 188.0], flat
    assert plain_report["lines"] == 0, plain_report

    boxes, a = page([2] * 5)
    titled, report = flow.space(a, boxes, zero,
                                roles=[layout.PARAGRAPH, layout.HEADING,
                                       layout.PARAGRAPH, layout.PARAGRAPH,
                                       layout.PARAGRAPH])
    # Full strength and the whole block, so a shortfall below is the rhythm's doing and
    # not the safety gate quietly reducing the plan.
    assert report["moved"] == 1 and report["reduced"] == 0, report
    got = _baselines(a, titled)

    prose_gap = got[3] - got[2]
    after_heading = got[2] - got[1]
    assert after_heading > prose_gap + 1e-6, (after_heading, prose_gap)
    assert abs(prose_gap - 20.0) < 1e-6, prose_gap

    # ...and it is the role that did it, not the page: the same geometry read as prose
    # left that gap at one plain pitch.
    assert after_heading > (flat[2] - flat[1]) + 1e-6, (after_heading, flat)
    print("  heading: prose under a heading is pushed past one plain pitch, by role alone")


def test_one_column_is_spaced_without_disturbing_the_other():
    """A correction in one column must not reach the column beside it.

    The left column has a single stretched gap and a 20pt rhythm; the right column is
    already even at 26pt, which is neither the left column's rhythm nor the page median.
    So the right column is only left alone if its target pitch is measured from its own
    lines. Anything shared — one block across the gutter, one page pitch used as every
    block's target — moves it, and moving it is the regression.
    """
    left_ys = [100.0] + [130.0 + 20.0 * r for r in range(23)]   # one 30pt gap, then 20pt
    right_ys = [100.0 + 26.0 * r for r in range(16)]            # even, on its own rhythm
    boxes, a = page([2] * len(left_ys), ys=left_ys)
    boxes, a = page([2] * len(right_ys), ys=right_ys, x0=110.0, into=(boxes, a.lines))
    assert len(a.blocks) == 2, f"fixture is not two columns: {a.blocks}"
    left, right = set(a.blocks[0]), set(a.blocks[1])
    assert len(left) == 24 and len(right) == 16, (sorted(left), sorted(right))

    for run in flow.blocks(a):
        assert set(run) <= left or set(run) <= right, f"block spans the gutter: {run}"
    assert len(flow.blocks(a)) == 2, flow.blocks(a)

    # The right column's own rhythm, not the page's: its targets are where it already is.
    want = flow.targets(a, a.blocks[1])
    here = [a.lines[k].baseline for k in a.blocks[1]]
    assert max(abs(w - h) for w, h in zip(want, here)) < 1e-9, (want, here)
    assert abs(a.pitch - 26.0) > 1.0, f"fixture lost its contrast: page pitch {a.pitch}"

    out, report = flow.space(a, boxes, [(0.0, 0.0)] * len(boxes))
    assert report["lines"] == 23, report        # every left line below the stretched gap

    stirred = {i for i, (dx, dy) in enumerate(out) if dx or dy}
    expected = {i for k in a.blocks[0][1:] for i in a.lines[k].indices}
    assert stirred == expected, sorted(stirred ^ expected)
    shifts = {out[i][1] for i in expected}
    assert len(shifts) == 1 and shifts.pop() < 0, shifts
    print("  columns: a column is spaced on its own rhythm and the next one stays put")


def test_an_equation_is_never_reshaped_and_never_spaced_through():
    """Two failures at once: a fraction re-spaced from the inside so the bar drifts off its
    numerator, and a block of prose normalised straight across the fraction as though the
    space above it and the space below it were the same rhythm.

    A fraction is the one place the instinct to normalise is destructive twice over. From
    the inside, the space between a numerator and its denominator is meaning rather than
    spacing. From the outside, prose above and prose below are not one flow once something
    atomic sits between them.
    """
    boxes, a, roles = maths_page()
    eq = a.lines[8].indices
    out, report = flow.space(a, boxes, [(0.0, 0.0)] * len(boxes), roles)

    assert {out[i] for i in eq} == {(0.0, 0.0)}, [out[i] for i in eq]

    def shape(bs):
        return [tuple(q - p for p, q in zip(bs[0], b)) for b in bs]

    # Stated against relative positions, not against the offsets, so it still holds a
    # frozen line to its shape if some later stage is allowed to translate one whole.
    moved = [collide._shift(boxes[i], *out[i]) for i in eq]
    assert shape(moved) == shape([boxes[i] for i in eq]), shape(moved)

    runs = flow.blocks(a, roles)
    assert all(8 not in run for run in runs), runs
    assert all(max(run) < 8 for run in runs), runs
    assert len(flow.blocks(a)) == 1, "the role is what splits the flow, not the geometry"

    for i in (i for i in range(len(boxes)) if i not in set(eq)):
        after = collide._shift(boxes[i], *out[i])
        for j in eq:
            assert collide._overlap(after, boxes[j]) == 0.0, (i, j, after, boxes[j])

    # The row immediately above wanted 5.8pt of correction and got none of it: proof the
    # equation stopped it rather than the page simply having nothing to do.
    assert out[6] == (0.0, 0.0) and out[7] == (0.0, 0.0), (out[6], out[7])
    assert report["reduced"] == 1 and report["lines"] > 0, report
    print("  equation: internal spacing untouched, and no prose is flowed through it")


def test_prose_is_never_spaced_onto_a_diagram():
    """Four prose lines whose last one is crowded, so the measured rhythm asks for it to
    drop, and a diagram occupying exactly the room it would drop into.

    Two failures are guarded. The first is the spacing plan being applied on its own
    authority, driving the closing line of a paragraph into the figure below it. The second
    is subtler and worse: the figure being given an offset of its own, because a frozen
    region that moves has lost the one guarantee that makes freezing it worth anything.
    """
    def fixture(top):
        """The same paragraph every time, with the figure's top edge as the only variable.

        The figure is deliberately wide and flat, so geometry reads it as a row of writing
        and puts it in the same flow block as the prose. Nothing but its role holds it
        still, which is what makes "the figure never moves" a claim worth asserting.
        """
        boxes, a = page([2, 2, 2, 2], ys=[100.0, 120.0, 140.0, 148.0])
        sketch = (10.0, top, 160.0, top + 18.0)
        boxes.append(sketch)
        a.lines.append(layout.Line(
            words=[layout.Word(indices=[len(boxes) - 1], box=sketch, baseline=sketch[3])],
            box=sketch, baseline=sketch[3]))
        a.n_boxes = len(boxes)
        return boxes, layout.statistics(a, boxes), sketch

    roles = [layout.PARAGRAPH] * 4 + [layout.DIAGRAM]
    clear = lambda bs, out, s: all(                                        # noqa: E731
        collide._overlap(collide._shift(b, *out[i]), s) == 0
        for i, b in enumerate(bs[:-1]))

    # What the stage wants before anything gates it. A fixture whose plan stopped short of
    # the figure by itself would prove nothing at all.
    boxes, a, sketch = fixture(158.0)
    block = flow.blocks(a, roles)[0]
    err = flow.targets(a, block, roles)[-1] - a.lines[block[-1]].baseline
    wanted = layout._correct(err, layout.BALANCED.line[0] * a.pitch, layout.BALANCED.line[1])
    assert collide._overlap(collide._shift(boxes[6], 0.0, wanted), sketch) > 0, wanted

    out, report = flow.space(a, boxes, [(0.0, 0.0)] * len(boxes), roles)
    assert clear(boxes, out, sketch), out
    assert out[-1] == (0.0, 0.0), f"the figure itself must never move: {out[-1]}"
    assert report["lines"] == 0, f"no reduction clears it, so nothing may be applied: {report}"

    # ...and with a little more room the move is cut down rather than abandoned, and what
    # lands still does not touch the figure.
    boxes, a, sketch = fixture(162.0)
    out, report = flow.space(a, boxes, [(0.0, 0.0)] * len(boxes), roles)
    assert report["lines"] == 1 and report["reduced"] == 1, report
    assert 0.0 < out[6][1] < wanted, out[6]
    assert out[6] == out[7], "a line moves as one piece or not at all"
    assert clear(boxes, out, sketch), out
    assert out[-1] == (0.0, 0.0), f"the figure itself must never move: {out[-1]}"
    print("  diagram: prose is stopped clear of a frozen figure, which never moves itself")


def test_a_dense_page_is_spaced_less_rather_than_spaced_wrongly():
    """The block asks for a move the page has no room for, so it must be cut down.

    Five rows with one stretched gap: the measured rhythm wants the bottom three lines
    raised by 9.6pt. Strips of unread ink sit 4pt above each of those rows, so the full
    request drives all six word boxes straight through them. What this proves is that the
    answer is a smaller move rather than the requested one: no block is applied whole, the
    applied shift is strictly smaller than the one asked for, and the finished page is no
    more entangled than the writer left it. Without the acceptance gate the same fixture
    reports the block moved and leaves six word boxes overlapping ink that never moves.
    """
    boxes, a = page([2] * 5, ys=[100.0, 120.0, 154.0, 174.0, 194.0])
    words = len(boxes)

    # The interline space is not empty on a real page. Two unclaimed strokes sit in each
    # of the gaps the lines are being asked to move into.
    for top in (146.0, 166.0, 186.0):
        for x in (10.0, 46.0):
            boxes.append((x, top, x + 30.0, top + 4.0))
    a.n_boxes = len(boxes)

    offsets = [(0.0, 0.0)] * len(boxes)
    out, report = flow.space(a, boxes, offsets)

    # What the rhythm actually asked for, so the fixture is proved to be a trap rather
    # than assumed to be one.
    block = flow.blocks(a)[0]
    want = flow.targets(a, block)
    asked = max(abs(t - a.lines[k].baseline) for k, t in zip(block, want))
    assert asked > 9.0, f"fixture no longer requests a real move: {asked}"
    naive = [(0.0, 0.0)] * 4 + [(0.0, -asked)] * 6 + [(0.0, 0.0)] * 6
    assert _entanglement(layout.moved(boxes, naive)), \
        "fixture is not dense: the full request fits, so there is nothing to refuse"

    assert report["moved"] == 0, f"no block here can be applied whole: {report}"
    assert report["reduced"] + report["dropped"] >= 1, report

    applied = max(abs(dy) for _, dy in out)
    assert applied < asked, f"the request was not cut down: {applied} vs {asked}"

    was = _entanglement(boxes)
    now = _entanglement(layout.moved(boxes, out))
    assert not was, "the fixture starts clean, so any overlap below is new"
    assert not now, f"spacing created overlap on a dense page: {sorted(now)}"

    # ...and the obstacles themselves are ink, not scenery: nothing may shove them.
    assert all(o == (0.0, 0.0) for o in out[words:]), out[words:]
    print("  dense: a page with no room gets a reduced move and no new overlap")


def test_two_lines_closing_on_each_other_are_judged_where_they_land():
    """The failure this exists for: a block whose lines are checked one at a time against
    the page as it stands today. Line 1 is asked whether it may come down while line 2 is
    still where the writer left it, line 2 is asked whether it may go up while line 1 is
    still where the writer left it, and both answers are yes. Applied together they close a
    gap neither of them was allowed to close, and the descender of one ends up inside the
    ascender of the other. The verdict has to be taken against the block's proposed layout.
    """
    # Baselines 100, 108, 128, 140, 152: a block pitch of 12 with one gap of 20 in it, so
    # the line above that gap is pushed down and the line below it pulled up.
    boxes, a = page([2] * 5, ys=[92.0, 100.0, 120.0, 132.0, 144.0])
    tail = stroke(boxes, a, 1, 0, (20.0, 104.0, 24.0, 113.0))
    head = stroke(boxes, a, 2, 0, (20.0, 116.5, 24.0, 124.0))
    assert collide._overlap(boxes[tail], boxes[head]) == 0.0, "the page starts clear"

    block = flow.blocks(a)[0]
    s = layout.BALANCED
    dy = [layout._correct(t - a.lines[k].baseline, s.line[0] * a.pitch, s.line[1])
          for k, t in zip(block, flow.targets(a, block))]
    assert dy[1] > 0 and dy[2] < 0, f"the fixture must have them closing: {dy}"

    zero = [(0.0, 0.0)] * len(boxes)
    assert collide.fits(a, boxes, zero, a.lines[1].indices, 1, 0.0, dy[1]), \
        "on its own, coming down is safe"
    assert collide.fits(a, boxes, zero, a.lines[2].indices, 2, 0.0, dy[2]), \
        "on its own, going up is safe"

    proposed = list(zero)
    for i in a.lines[2].indices:
        proposed[i] = (0.0, dy[2])
    assert not collide.fits(a, boxes, proposed, a.lines[1].indices, 1, 0.0, dy[1]), \
        "against where line 2 ENDS UP, coming down is not safe"

    out, report = flow.space(a, boxes, zero, s=s)
    assert report["moved"] == 0, f"the block cannot be applied whole: {report}"
    assert collide._overlap(collide._shift(boxes[tail], *out[tail]),
                            collide._shift(boxes[head], *out[head])) == 0.0, \
        (out[tail], out[head])
    print("  simultaneous: a block is gated on its proposed layout, not its current one")


if __name__ == "__main__":
    for fn in [test_ragged_prose_gets_a_more_even_rhythm,
               test_a_comma_travels_with_the_line_it_hangs_off,
               test_a_stroke_between_two_lines_is_neither_adopted_nor_stranded,
               test_a_heading_is_given_more_room_below_it,
               test_one_column_is_spaced_without_disturbing_the_other,
               test_an_equation_is_never_reshaped_and_never_spaced_through,
               test_prose_is_never_spaced_onto_a_diagram,
               test_a_dense_page_is_spaced_less_rather_than_spaced_wrongly,
               test_two_lines_closing_on_each_other_are_judged_where_they_land]:
        print(fn.__name__)
        fn()
    print("\nall checks passed")

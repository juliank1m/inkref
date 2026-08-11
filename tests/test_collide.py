"""Moving ink must never make the page more tangled than the writer left it.

Every other check in this project measures whether the layout got *better*. This one
measures whether it stayed *safe*, which is a different question with a different answer:
a page can score better on every spread and still have a word sitting on top of a diagram.

Run: python3 tests/test_collide.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.ink import collide, layout      # noqa: E402


def page(rows, x0=10.0, height=8.0, pitch=20.0, width=30.0, gap=6.0):
    """A tidy grid of word boxes -> (boxes, Analysis). `rows` is words per row."""
    boxes, lines = [], []
    for r, count in enumerate(rows):
        words, y = [], 100.0 + r * pitch
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


def test_a_move_into_empty_space_is_untouched():
    boxes, a = page([2, 2])
    offsets = [(0.0, 0.0)] * len(boxes)
    offsets[0] = (3.0, 0.0)          # into the gap before the next word
    out, report = collide.constrain(a, boxes, offsets)
    assert out[0] == (3.0, 0.0), out[0]
    assert report["reduced"] == 0 and report["cancelled"] == 0, report
    print("  clear: a move with room to make is passed through unchanged")


def test_a_move_into_unread_ink_is_stopped():
    """The failure this exists for: ink no group claims is invisible to the planner."""
    boxes, a = page([1])
    # a margin note nobody recognised, sitting just right of the only word
    boxes = boxes + [(45.0, 100.0, 55.0, 108.0)]
    a.n_boxes = len(boxes)
    offsets = [(0.0, 0.0)] * len(boxes)
    offsets[0] = (12.0, 0.0)         # would drive the word straight through it
    out, report = collide.constrain(a, boxes, offsets)
    assert abs(out[0][0]) < 12.0, out[0]
    assert report["reduced"] + report["cancelled"] == 1, report
    assert out[1] == (0.0, 0.0), "the obstacle itself must never move"
    print("  obstacle: a move into ink no group owns is reduced or cancelled")


def test_ink_that_already_overlaps_is_not_frozen():
    """Letters overlap, descenders reach into the line below, dense notes touch
    everywhere. A test that vetoed contact would veto the whole page."""
    boxes, a = page([2])
    boxes[1] = (boxes[0][2] - 4.0, boxes[1][1], boxes[1][2], boxes[1][3])   # already
    a.lines[0].words[1] = layout.Word(indices=[1], box=boxes[1],
                                      baseline=boxes[1][3])
    offsets = [(0.0, 0.0), (2.0, 0.0)]      # moving APART, so overlap shrinks
    out, _ = collide.constrain(a, boxes, offsets)
    assert out[1] == (2.0, 0.0), out[1]
    print("  contact: existing overlap is allowed to persist, only new overlap is refused")


def test_the_page_edge_is_a_wall():
    boxes, a = page([1])
    offsets = [(0.0, 0.0)] * len(boxes)
    offsets[0] = (-40.0, 0.0)               # off the left edge
    out, report = collide.constrain(a, boxes, offsets, page=(200.0, 300.0))
    assert out[0][0] > -40.0, out[0]
    assert report["reduced"] + report["cancelled"] == 1, report
    print("  bounds: a move off the page is reduced to fit or cancelled")


def test_a_protected_region_may_not_be_approached():
    """An equation is atomic: not merely un-overlapped, but not encroached on.

    The guarantee is about the *result*, not about how far the move was cut. Reducing a
    move until it clears is the intended outcome; cancelling it is what happens when no
    reduction clears, which is what the tight fixture below forces.
    """
    boxes, a = page([1, 1], pitch=12.0)     # rows only 4pt apart
    offsets = [(0.0, 0.0)] * len(boxes)
    offsets[0] = (0.0, 18.0)                # down onto the equation below
    roles = [layout.PARAGRAPH, layout.EQUATION]
    out, report = collide.constrain(a, boxes, offsets, roles)
    assert out[0] == (0.0, 0.0), f"no reduction clears it, so it must be cancelled: {out[0]}"
    assert report["cancelled"] == 1, report

    # ...and with room to reduce into, it reduces, and the result still does not touch.
    boxes, a = page([1, 1], pitch=40.0)
    offsets = [(0.0, 0.0)] * len(boxes)
    offsets[0] = (0.0, 38.0)
    out, _ = collide.constrain(a, boxes, offsets, roles)
    assert 0 < out[0][1] < 38.0, out[0]
    moved = collide._shift(boxes[0], *out[0])
    assert collide._overlap(moved, boxes[1]) == 0, (moved, boxes[1])
    print("  protected: an equation is never reached — the move is cut, or dropped")


def test_the_gate_can_only_soften_a_plan():
    """Whatever else it does, it must never invent or amplify a move."""
    boxes, a = page([3, 3, 3])
    offsets = [(1.5 * (i % 3) - 1.0, 0.7) for i in range(len(boxes))]
    out, _ = collide.constrain(a, boxes, offsets)
    assert len(out) == len(offsets)
    for (dx, dy), (ox, oy) in zip(out, offsets):
        assert abs(dx) <= abs(ox) + 1e-9 and abs(dy) <= abs(oy) + 1e-9, ((dx, dy), (ox, oy))
        assert dx * ox >= -1e-9 and dy * oy >= -1e-9, "direction must never flip"
    print("  monotone: every offset comes back same-signed and no larger")


def test_no_plan_is_a_no_op():
    boxes, a = page([2, 2])
    zero = [(0.0, 0.0)] * len(boxes)
    out, report = collide.constrain(a, boxes, zero)
    assert out == zero and report["groups"] == 0, report
    empty = layout.Analysis(n_boxes=0)
    assert collide.constrain(empty, [], [])[0] == []
    print("  degenerate: an empty plan and an empty page are both quiet")


def test_the_index_finds_what_a_scan_would():
    """The grid is an optimisation; if it disagrees with brute force it is a bug that
    only shows up as a collision nobody caught."""
    boxes = [(x * 7.0, y * 5.0, x * 7.0 + 6.0, y * 5.0 + 4.0)
             for x in range(12) for y in range(9)]
    ink = collide.InkMap(boxes, ref_h=4.0)
    for probe in (boxes[0], boxes[50], (30.0, 20.0, 45.0, 33.0), (-5.0, -5.0, 2.0, 2.0)):
        near = ink.near(probe, pad=3.0)
        brute = {i for i, b in enumerate(boxes)
                 if collide._overlap((probe[0] - 3, probe[1] - 3,
                                      probe[2] + 3, probe[3] + 3), b) > 0}
        assert brute <= near, sorted(brute - near)
    print("  index: the grid never misses a box a linear scan would have found")


if __name__ == "__main__":
    for fn in [test_a_move_into_empty_space_is_untouched,
               test_a_move_into_unread_ink_is_stopped,
               test_ink_that_already_overlaps_is_not_frozen,
               test_the_page_edge_is_a_wall,
               test_a_protected_region_may_not_be_approached,
               test_the_gate_can_only_soften_a_plan,
               test_no_plan_is_a_no_op,
               test_the_index_finds_what_a_scan_would]:
        print(fn.__name__)
        fn()
    print("\nall checks passed")

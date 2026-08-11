"""The bridge from recognised text back to the user's own strokes.

The recogniser itself is not under test here — it is a black box that will read a page
differently on a different OS, and demanding a fixed transcription would be a test of Apple
rather than of this project. What *is* tested is everything between it and the planner,
because that is the part that can be silently wrong: a box converted with the wrong y-axis
lands somewhere plausible, and the page just comes out looking much like it went in.

Run: python3 tests/test_recognize.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.ink import grouping, layout, recognize      # noqa: E402
from inkref.ink.recognize import (PageTransform, RecognizedLine,      # noqa: E402
                                  RecognizedWord)


def line(text, x0, y0, x1, y1, words=None, conf=1.0):
    return RecognizedLine(text=text, box=(x0, y0, x1, y1), confidence=conf,
                          words=words or [RecognizedWord(text, (x0, y0, x1, y1), conf)])


def words_at(*spans):
    return [RecognizedWord(t, (x0, y0, x1, y1), 1.0) for t, x0, y0, x1, y1 in spans]


def test_transform_is_its_own_inverse_at_the_corners():
    """The invariant the whole pipeline rests on: normalised (0,0)-(1,1) IS the rectangle.

    Vision's origin is bottom-left and y-up; the page's is top-left and y-down. Getting
    that backwards flips every box about the page centre — which looks like a plausible
    layout, not like a crash, and is exactly why this is pinned.
    """
    t = PageTransform(width=600.0, height=800.0, scale=5.0)
    assert t.from_normalized(0, 0, 1, 1) == (0.0, 0.0, 600.0, 800.0)
    assert t.pixels == (3000, 4000)

    # bottom-left tenth in Vision's frame is the BOTTOM of the page in ours
    x0, y0, x1, y1 = t.from_normalized(0, 0, 0.1, 0.1)
    assert (x0, x1) == (0.0, 60.0)
    assert (y0, y1) == (720.0, 800.0), (y0, y1)

    # a tile carries its own origin, so a box lands on the page and not on the tile
    tile = PageTransform(width=100.0, height=200.0, scale=5.0, x0=300.0, y0=400.0)
    assert tile.from_normalized(0, 0, 1, 1) == (300.0, 400.0, 400.0, 600.0)
    print("  transform: normalised corners map to the rectangle, y flips, tiles offset")


def test_a_stroke_belongs_to_exactly_one_group():
    """Two groups sharing a stroke would translate the same ink twice and tear it."""
    lines = [line("one two", 0, 0, 100, 10,
                  words_at(("one", 0, 0, 40, 10), ("two", 50, 0, 100, 10))),
             # deliberately overlapping the first, as tiles produce
             line("one", 0, 1, 45, 11, words_at(("one", 0, 1, 45, 11)))]
    boxes = [(5.0, 2.0, 15.0, 9.0), (20.0, 2.0, 35.0, 9.0), (55.0, 2.0, 95.0, 9.0)]
    groups, unmatched = grouping.map_strokes(lines, boxes)
    seen = [i for g in groups for i in g.indices]
    assert len(seen) == len(set(seen)), f"a stroke was claimed twice: {seen}"
    assert set(seen) | set(unmatched) == set(range(len(boxes)))
    print("  mapping: every stroke lands in at most one group, none is lost")


def test_unmatched_strokes_are_never_moved():
    """The safety property. A diagram the recogniser ignores must come back untouched."""
    lines = [line("text", 0, 0, 100, 10, words_at(("text", 0, 0, 100, 10))),
             line("more", 0, 20, 100, 30, words_at(("more", 0, 20, 100, 30)))]
    boxes = [(5.0, 1.0, 40.0, 9.0), (50.0, 1.0, 95.0, 9.0),
             (5.0, 21.0, 40.0, 29.0), (50.0, 21.0, 95.0, 29.0),
             (200.0, 200.0, 260.0, 260.0)]          # a sketch, far away
    groups, unmatched = grouping.map_strokes(lines, boxes)
    assert unmatched == [4], unmatched
    a = grouping.analysis(groups, boxes)
    assert a.n_boxes == len(boxes), "offsets must stay parallel to the caller's list"
    offsets = layout.plan(a, layout.STRONG)
    assert len(offsets) == len(boxes)
    assert offsets[4] == (0.0, 0.0), "an unmatched stroke was moved"
    print("  safety: a stroke no group claimed gets a zero offset, even at full strength")


def test_overlapping_readings_merge_into_one_line():
    """An exponent is read as its own line, overlapping the run it belongs to. Left apart,
    the planner spaces it away from its base and calls that gap the page's line pitch."""
    body = line("f(x) = x", 100, 40, 160, 52)
    exponent = line("2", 158, 36, 164, 44)           # overlaps the body's top
    across = line("other column", 400, 41, 500, 51)  # same height, a gutter away
    merged = recognize.merge_stacked([body, exponent, across])
    assert len(merged) == 2, [l.text for l in merged]
    joined = [l for l in merged if l.box[0] == 100][0]
    assert (joined.box[1], joined.box[3]) == (36, 52), joined.box
    assert len(joined.words) == 2, "the merged line keeps both readings' words"
    print("  merge: an exponent rejoins its base; a column across the page stays separate")


def test_consecutive_prose_lines_are_never_merged():
    """The boundary of the rule above. Two lines that merely sit close must stay two
    lines, or a paragraph collapses into one unspaceable block."""
    first = line("the first line of prose", 0, 40, 200, 50)
    second = line("the second line of prose", 0, 52, 200, 62)
    assert len(recognize.merge_stacked([first, second])) == 2
    print("  merge: lines that do not overlap stay apart, however close")


def test_dedupe_prefers_the_whole_confident_line():
    whole = line("the whole line", 0, 0, 200, 10, conf=0.9)
    fragment = line("whole", 40, 0, 120, 10, conf=1.0)
    kept = recognize.dedupe([fragment, whole])
    assert len(kept) == 1 and kept[0].text == "the whole line", [l.text for l in kept]
    print("  dedupe: an overlapping fragment loses to the line that contains it")


def test_recognised_structure_beats_geometry_on_a_tight_gap():
    """Why any of this exists. Two words a hair apart are one word to geometry and two
    words to a recogniser, and only the recogniser is right."""
    boxes = [(0.0, 0.0, 20.0, 10.0), (21.0, 0.0, 40.0, 10.0),
             (0.0, 20.0, 20.0, 30.0), (21.0, 20.0, 40.0, 30.0)]
    geo = layout.analyze(boxes)
    assert all(len(l.words) == 1 for l in geo.lines), "fixture no longer tests anything"

    lines = [line("a b", 0, 0, 40, 10,
                  words_at(("a", 0, 0, 20, 10), ("b", 21, 0, 40, 10))),
             line("c d", 0, 20, 40, 30,
                  words_at(("c", 0, 20, 20, 30), ("d", 21, 20, 40, 30)))]
    groups, _ = grouping.map_strokes(lines, boxes)
    a = grouping.analysis(groups, boxes)
    assert [len(l.words) for l in a.lines] == [2, 2], [len(l.words) for l in a.lines]
    print("  recognition: a gap geometry cannot judge is split correctly by reading")


def test_no_recogniser_is_not_an_error():
    """pyobjc is macOS-only; the rest of the project must stay runnable without it."""
    groups, unmatched = grouping.map_strokes([], [(0.0, 0.0, 1.0, 1.0)])
    assert groups == [] and unmatched == [0]
    a = grouping.analysis([], [(0.0, 0.0, 1.0, 1.0)])
    assert a.lines == [] and a.n_boxes == 1
    print("  degraded: no lines means no groups, no plan, and no exception")


def test_live_recogniser_reads_a_rendered_line():
    """End to end, if this machine has Vision: render text, read it, land it on the page.

    Skipped rather than failed elsewhere — the point of the interface is that the
    recogniser is swappable, including for nothing at all.
    """
    reader = recognize.recognizer()
    if reader is None:
        print("  live: no recogniser on this machine — skipped")
        return
    from inkref.goodnotes import render
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120" '
           'viewBox="0 0 400 120"><rect width="400" height="120" fill="white"/>'
           '<text x="20" y="80" font-family="Helvetica" font-size="56" fill="black">'
           'Hello world</text></svg>')
    png = render.to_png(svg, scale=2.0)
    if png is None:
        print("  live: no rasteriser — skipped")
        return
    t = PageTransform(width=400.0, height=120.0, scale=2.0)
    lines = reader.recognize(png, t)
    assert lines, "the recogniser found nothing in a page of 56pt text"
    box = lines[0].box
    # It must land on the text, not on the page's other half: the y-flip is the bug this
    # catches, and it would put the box near y=0 instead of near the baseline at y=80.
    assert 20 <= box[1] <= 90 and 40 <= box[3] <= 110, box
    assert box[0] < 120 and box[2] > 150, box
    print(f"  live: read {lines[0].text!r} at "
          f"({box[0]:.0f},{box[1]:.0f})-({box[2]:.0f},{box[3]:.0f})")


if __name__ == "__main__":
    for fn in [test_transform_is_its_own_inverse_at_the_corners,
               test_a_stroke_belongs_to_exactly_one_group,
               test_unmatched_strokes_are_never_moved,
               test_overlapping_readings_merge_into_one_line,
               test_consecutive_prose_lines_are_never_merged,
               test_dedupe_prefers_the_whole_confident_line,
               test_recognised_structure_beats_geometry_on_a_tight_gap,
               test_no_recogniser_is_not_an_error,
               test_live_recogniser_reads_a_rendered_line]:
        print(fn.__name__)
        fn()
    print("\nall checks passed")

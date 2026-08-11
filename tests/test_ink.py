"""Checks for the intermediate representation and the GoodNotes writer adapter.

Run: python3 tests/test_ink.py     (stdlib only)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes import strokes as strokes_mod          # noqa: E402
from inkref.goodnotes.document import Document               # noqa: E402
from inkref.goodnotes.writer import GoodNotesWriter, TemplateError, reader_to_ink  # noqa: E402
from inkref.ink.model import Color, InkDocument, InkPage, InkStroke  # noqa: E402

TEMPLATE = os.path.join(ROOT, "samples", "test.goodnotes")
OUT = os.path.join(ROOT, "generated", "_ink_test.goodnotes")


def test_model_invariants():
    try:
        InkStroke(points=[(0, 0)])
    except ValueError:
        pass
    else:
        raise AssertionError("a 1-point stroke should be rejected")
    try:
        InkStroke(points=[(0, 0), (1, 1)], widths=[1.0])
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched widths should be rejected")
    print("  model: degenerate strokes rejected")


def test_geometry_helpers():
    s = InkStroke(points=[(0, 0), (10, 20)], width=2.0)
    assert s.bounds == (0, 0, 10, 20)
    assert s.translated(5, -5).points == [(5, -5), (15, 15)]
    assert s.translated(5, -5).points != s.points, "translate must not mutate in place"

    big = s.scaled(2)
    assert big.points == [(0, 0), (20, 40)]
    assert big.width == 4.0, "scaling must scale width too"

    page = InkPage()
    page.add(s)
    page.add(InkStroke(points=[(-5, 3), (0, 4)]))
    assert len(page) == 2
    assert page.bounds == (-5, 0, 10, 20)
    print("  model: bounds, translate, scale, page aggregation")


def test_color():
    assert Color.from_hex("#ff8000").as_tuple()[:3] == (1.0, 128 / 255, 0.0)
    assert Color.gray(0.5).as_tuple() == (0.5, 0.5, 0.5, 1.0)
    print("  colour: hex and gray constructors")


def test_width_scale_is_calibrated():
    """Width uses 1/144 inch, coordinates use 1/132. They are NOT the same unit.

    Calibrated against ten strokes from 0.5 to 24 units exported through Goodnotes:
    every one measured at exactly units/2 points. Regression guard against anyone
    "unifying" this with the 11/6 coordinate scale.
    """
    from inkref.goodnotes import writer
    from inkref.goodnotes.document import UNITS_PER_POINT
    assert writer.WIDTH_UNITS_PER_POINT == 2.0
    assert abs(UNITS_PER_POINT - 11 / 6) < 1e-12
    assert writer.WIDTH_UNITS_PER_POINT != UNITS_PER_POINT

    for pt, units in [(0.25, 0.5), (1.5, 3.0), (12.0, 24.0)]:
        assert abs(pt * writer.WIDTH_UNITS_PER_POINT - units) < 1e-9
    print("  width: 2 units per point, distinct from the 11/6 coordinate scale")


def test_write_and_read_back():
    """InkDocument -> .goodnotes -> InkDocument must survive geometry, colour and width."""
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    page = InkPage(width=595, height=842)
    made = [
        InkStroke(points=[(100, 100), (120, 100), (140, 100), (160, 100)],
                  color=Color.from_hex("d62828"), width=2.0),
        InkStroke(points=[(100 + 20 * i, 200 + (30 if i % 2 else 0)) for i in range(7)],
                  color=Color.from_hex("1d4ed8"), width=3.5),
    ]
    for s in made:
        page.add(s)
    ink = InkDocument(title="test")
    ink.add_page(page)

    _, written = GoodNotesWriter(TEMPLATE).write(ink, OUT)
    assert written == len(made)

    back = reader_to_ink(OUT)
    # the template's own stroke also has 7 points, so identify ours by colour
    wanted = {s.color.as_tuple() for s in made}
    ours = [s for p in back.pages for s in p.strokes
            if any(all(abs(a - b) < 0.01 for a, b in zip(w, s.color.as_tuple()))
                   for w in wanted)]
    assert len(ours) == 2, f"expected our 2 strokes back, got {len(ours)}"

    for want, have in zip(made, sorted(ours, key=lambda s: len(s.points))):
        assert len(have.points) == len(want.points)
        for (ax, ay), (bx, by) in zip(want.points, have.points):
            assert abs(ax - bx) < 0.01 and abs(ay - by) < 0.01, ((ax, ay), (bx, by))
        assert abs(have.width - want.width) < 0.01, (have.width, want.width)
        for a, b in zip(want.color.as_tuple(), have.color.as_tuple()):
            assert abs(a - b) < 0.01, (want.color, have.color)
    print("  writer: round-trips geometry, colour and width through points<->units")


def test_written_records_are_valid():
    doc = Document.open(OUT)
    page = next(p for p in doc.pages if p.live)
    for rec in page.records:
        assert rec.is_consistent(), f"{rec.uuid}: descriptor/item mismatch"
    for rec in page.live:
        sig, _ = rec.geometry
        if sig == strokes_mod.CONSTANT_WIDTH:
            assert rec.family_marker is None
            assert not rec.deleted
    print("  writer: every emitted record satisfies the GoodNotes invariants")


def test_clear_existing_keeps_everything_that_is_not_a_live_stroke():
    """SPEC §15 / §19 test 5: content we do not understand survives the round trip.

    Regression. `clear_existing` used to keep only the structural template and drop every
    other entry on the page — which silently deleted tombstones and any non-pen-stroke
    item (image, text box, math group), all of which this code carries as opaque bytes.
    The samples are pure stroke pages, so nothing ever noticed.
    """
    from inkref.goodnotes import protobuf as pb
    from inkref.goodnotes import records

    doc = Document.open(TEMPLATE)
    page = next(p for p in doc.pages if p.live)
    foreign = (pb.bytes_field(1, b"NOT-A-STROKE"),
               pb.bytes_field(records.TEXT_BOX, b"opaque payload"))
    page.entries.append(foreign)
    tombstones = [r for r in page.records if r.deleted]
    assert tombstones, "fixture should contain a tombstone"
    doc.write(OUT)

    ink = InkDocument()
    p = ink.add_page(InkPage())
    p.add(InkStroke(points=[(50, 50), (120, 50)], width=2.0))

    GoodNotesWriter(OUT).write(ink, OUT + ".2", clear_existing=True)
    after = Document.open(OUT + ".2")
    page2 = next(p for p in after.pages if p.records)

    survived = [e for e in page2.entries if not isinstance(e, records.StrokeRecord)]
    assert foreign in survived, "a non-pen-stroke entry was dropped"
    assert len([r for r in page2.records if r.deleted]) == len(tombstones), \
        "a tombstone was dropped"
    assert len(page2.live) == 1, f"expected only our stroke to be live, got {len(page2.live)}"
    os.remove(OUT + ".2")
    print("  writer: clear_existing removes live strokes only, not tombstones or "
          "unknown objects")


def test_template_errors_are_explicit():
    ink = InkDocument()
    for _ in range(9):
        p = InkPage()
        p.add(InkStroke(points=[(0, 0), (1, 1)]))
        ink.add_page(p)
    try:
        GoodNotesWriter(TEMPLATE).write(ink, OUT)
    except TemplateError as e:
        assert "pages" in str(e)
    else:
        raise AssertionError("too many pages should raise TemplateError")
    print("  writer: template shortfall raises instead of silently dropping pages")


if __name__ == "__main__":
    for fn in [test_model_invariants, test_geometry_helpers, test_color,
               test_width_scale_is_calibrated,
               test_write_and_read_back, test_written_records_are_valid,
               test_clear_existing_keeps_everything_that_is_not_a_live_stroke,
               test_template_errors_are_explicit]:
        print(fn.__name__)
        fn()
    if os.path.exists(OUT):
        os.remove(OUT)
    print("\nall checks passed")

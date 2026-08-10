"""Checks for PDF extraction.

Geometry helpers run on stdlib alone. The end-to-end checks need PyMuPDF and the sample
export, and skip cleanly when either is missing.

Run: ./venv/bin/python tests/test_pdf.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkport.goodnotes.document import POINTS_PER_UNIT   # noqa: E402
from inkport.pdf import extract as ex                    # noqa: E402

SAMPLE = os.path.join(ROOT, "samples", "pdf", "03c_synthetic_stroke.pdf")

# what milestone 3 authored, in GoodNotes units
AUTHORED_LINE = [(100.0, 100.0), (160.0, 100.0)]
AUTHORED_ZIGZAG = [(100.0 + 40 * i, 220.0 + (60.0 if i % 2 else 0.0)) for i in range(9)]


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def test_dedupe():
    assert ex._dedupe([(0, 0), (0, 0), (1, 1), (1, 1), (2, 2)]) == [(0, 0), (1, 1), (2, 2)]
    assert ex._dedupe([]) == []
    print("  dedupe: repeated vertices collapsed")


def test_simplify_keeps_shape():
    # a dense straight line must collapse to its endpoints
    line = [(i, 0.0) for i in range(50)]
    assert ex.simplify(line, 0.25) == [(0, 0.0), (49, 0.0)]

    # a corner must survive
    corner = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (10.0, 5.0), (10.0, 10.0)]
    out = ex.simplify(corner, 0.25)
    assert (10.0, 0.0) in out, "the corner vertex must be kept"
    assert len(out) == 3, out

    # tolerance 0 is a no-op, and endpoints are always preserved
    assert ex.simplify(corner, 0.0) == corner
    for tol in (0.1, 1.0, 10.0):
        s = ex.simplify(corner, tol)
        assert s[0] == corner[0] and s[-1] == corner[-1]

    # a stroke that is already minimal is untouched
    assert ex.simplify([(0, 0), (1, 1)], 0.25) == [(0, 0), (1, 1)]
    print("  simplify: collinear points dropped, corners and endpoints kept")


def test_extraction_matches_authored_geometry():
    """The sample was authored by us, so ground truth is exact."""
    if ex.fitz is None:
        print("  SKIP: PyMuPDF not installed")
        return
    if not os.path.exists(SAMPLE):
        print(f"  SKIP: {os.path.relpath(SAMPLE, ROOT)} not present")
        return

    doc = ex.extract(SAMPLE)
    assert len(doc.pages) == 1
    page = doc.pages[0]
    assert abs(page.width - 595.3) < 0.5 and abs(page.height - 841.9) < 0.5
    assert len(page) == 3, f"expected 3 /Ink strokes, got {len(page)}"

    for authored, name in ((AUTHORED_LINE, "line"), (AUTHORED_ZIGZAG, "zigzag")):
        want = _bbox([(x * POINTS_PER_UNIT, y * POINTS_PER_UNIT) for x, y in authored])
        best = min(page.strokes,
                   key=lambda s: sum(abs(a - b) for a, b in zip(s.bounds, want)))
        err = max(abs(a - b) for a, b in zip(best.bounds, want))
        # GoodNotes' exporter insets endpoints by up to about half a stroke width
        assert err < 2.5, f"{name}: placement off by {err:.2f} pt"

    # colours must survive the round trip exactly
    hexes = {f"{int(s.color.r*255):02x}{int(s.color.g*255):02x}{int(s.color.b*255):02x}"
             for s in page.strokes}
    assert "1959e5" in hexes and "d82626" in hexes, hexes

    # an ordinary 3 pt pen must not be classified as a highlighter
    pens = [s for s in page.strokes if s.kind == "pen"]
    assert len(pens) == len(page.strokes), \
        f"{len(page.strokes) - len(pens)} stroke(s) misclassified as highlighter"
    print(f"  extraction: 3 strokes, placement within 2.5 pt, colours exact, "
          f"no false highlighters")


def test_simplification_does_not_move_the_stroke():
    """Tolerance must trade point count for size, never for position."""
    if ex.fitz is None or not os.path.exists(SAMPLE):
        print("  SKIP: sample or PyMuPDF unavailable")
        return
    base = ex.extract(SAMPLE, tolerance=0.0).pages[0]
    for tol in (0.05, 0.25, 1.0):
        got = ex.extract(SAMPLE, tolerance=tol).pages[0]
        assert len(got) == len(base)
        for a, b in zip(base.strokes, got.strokes):
            assert len(b.points) <= len(a.points)
            drift = max(abs(x - y) for x, y in zip(a.bounds, b.bounds))
            assert drift <= tol + 1e-6, f"tol={tol} moved the bbox by {drift:.3f} pt"
    print("  simplify: bbox drift stays within the requested tolerance")


if __name__ == "__main__":
    for fn in [test_dedupe, test_simplify_keeps_shape,
               test_extraction_matches_authored_geometry,
               test_simplification_does_not_move_the_stroke]:
        print(fn.__name__)
        fn()
    print("\nall checks passed")

"""Milestone 6 — calibrate stroke width.

Coordinates scale by exactly 11/6. Width does not, and the evidence conflicts:

    constant width, authored 6.0 units  ->  PDF w = 3.00     ratio 2.00
    variable width, stored   ~0.20      ->  PDF w = 0.2054   ratio ~1.03

One data point each is not a calibration. This writes a page of horizontal lines at known
widths so a single export settles it.

Workflow:
    1. ./venv/bin/python milestones/m6_width_calibration.py          -> generated/06_widths.goodnotes
    2. import it, then export that notebook from Goodnotes as an Editable PDF
    3. ./venv/bin/python milestones/m6_width_calibration.py <that.pdf>   -> the ratio table
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkport.goodnotes.document import Document, POINTS_PER_UNIT   # noqa: E402
from inkport.goodnotes.strokes import Stroke                       # noqa: E402

TEMPLATE = os.path.join(ROOT, "samples", "test.goodnotes")
OUTPUT = os.path.join(ROOT, "generated", "06_widths.goodnotes")

# GoodNotes units. Spread wide enough that any linear or affine relation is visible.
WIDTHS = [0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0]
X0, X1, Y0, DY = 100.0, 500.0, 120.0, 110.0


def build():
    doc = Document.open(TEMPLATE)
    page = next(p for p in doc.pages if p.live)
    for i, w in enumerate(WIDTHS):
        y = Y0 + i * DY
        doc.add_stroke(page.id, Stroke(points=[(X0, y), (X1, y)],
                                       color=(0.1, 0.1, 0.1, 1.0), width=w))
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    doc.write(OUTPUT)
    print(f"wrote {os.path.relpath(OUTPUT, ROOT)}")
    print(f"  {len(WIDTHS)} horizontal lines, top to bottom, widths in GoodNotes units:")
    for i, w in enumerate(WIDTHS):
        print(f"    y={Y0 + i * DY:6.1f}u ({(Y0 + i * DY) * POINTS_PER_UNIT:6.1f}pt)  "
              f"width {w:5.1f}u   -> expected {w * POINTS_PER_UNIT:5.2f}pt if width "
              f"scaled like coordinates")
    print("\nImport it, export as an Editable PDF, then re-run with that PDF as an argument.")


def measure(pdf_path):
    from inkport.pdf import extract as ex
    if ex.fitz is None:
        sys.exit("needs PyMuPDF")
    doc = ex.fitz.open(pdf_path)
    rows = []
    for page in doc:
        for annot in page.annots() or []:
            if annot.type[1] != "Ink":
                continue
            v = annot.vertices or []
            pts = v[0] if v and isinstance(v[0], list) else v
            if len(pts) < 2:
                continue
            ys = [p[1] for p in pts]
            xs = [p[0] for p in pts]
            if max(ys) - min(ys) > 2.0:      # keep only the horizontal calibration lines
                continue
            rows.append((sum(ys) / len(ys), (annot.border or {}).get("width"),
                         max(xs) - min(xs)))
    doc.close()
    rows.sort()
    if len(rows) != len(WIDTHS):
        print(f"!! found {len(rows)} horizontal strokes, expected {len(WIDTHS)} — "
              f"matching by vertical order anyway")
    print(f"{'authored (u)':>13} {'expected pt':>12} {'measured w':>11} "
          f"{'ratio u/pt':>11} {'vs 11/6':>9}")
    ratios = []
    for (y, w, span), authored in zip(rows, WIDTHS):
        if not w:
            continue
        ratio = authored / w
        ratios.append(ratio)
        print(f"{authored:13.1f} {authored * POINTS_PER_UNIT:12.2f} {w:11.3f} "
              f"{ratio:11.3f} {ratio / (11 / 6):9.3f}")
    if ratios:
        mean = sum(ratios) / len(ratios)
        spread = max(ratios) - min(ratios)
        print(f"\nmean ratio units-per-point = {mean:.4f}   spread {spread:.4f}")
        print(f"  11/6 = {11/6:.4f}   current constant WIDTH_UNITS_PER_POINT = 2.0")
        print("  A tight spread means one linear factor; a drifting ratio means the "
              "relation is not proportional (offset, clamping, or quantised pen sizes).")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        measure(sys.argv[1])
    else:
        build()

"""GoodNotes "Editable" PDF -> InkDocument.

Ink lives in `/Ink` annotations, one per stroke, each carrying `/InkList`: the centerline
polyline. That is the easy case — no skeletonization, no content-stream tokenizing, no
transform chain to compose.

CONFIRMED against a real export (Goodnotes 7.0.34 -> Quartz PDFContext):
  - one /Ink annotation per stroke
  - /InkList is populated and is a flattened centerline polyline in page points
  - PyMuPDF returns those vertices already in page space, y-down from the top-left,
    matching the IR — no flip needed
  - stroke colour round-trips bit-exact
  - GoodNotes flattens curves densely: a 4-point authored stroke came back as 31 points,
    a 9-point one as 81. Simplify, or a round trip inflates ~10x.

Needs PyMuPDF (see venv/).
"""
import sys

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None

from ..ink.model import Color, InkDocument, InkPage, InkStroke

# Highlighter classification. Alpha is the reliable signal; width alone is not — a 3 pt pen
# is perfectly normal and was being misclassified by the 2.5 pt threshold borrowed from
# handwriting-neatener. Width is kept only as a weak fallback at a value no pen reaches.
# UNCALIBRATED: no real highlighter sample has been examined yet.
HIGHLIGHTER_MIN_WIDTH_PT = 8.0
HIGHLIGHTER_MAX_ALPHA = 0.99


def _vertices(annot):
    """-> list of subpaths, each a list of (x, y) in page points."""
    v = annot.vertices
    if not v:
        return []
    if v and isinstance(v[0], (list, tuple)) and v[0] and isinstance(v[0][0], (list, tuple)):
        return [[tuple(p) for p in sub] for sub in v]
    return [[tuple(p) for p in v]]


def _dedupe(points, eps=1e-6):
    out = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    return out


def simplify(points, tolerance=0.25):
    """Ramer-Douglas-Peucker. GoodNotes exports densely flattened polylines; without this
    a round trip multiplies the point count by ~10 for no visual gain."""
    if tolerance <= 0 or len(points) < 3:
        return list(points)

    def rdp(pts):
        if len(pts) < 3:
            return list(pts)
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5
        worst, idx = -1.0, 0
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            if norm == 0:
                d = ((px - x0) ** 2 + (py - y0) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
            if d > worst:
                worst, idx = d, i
        if worst <= tolerance:
            return [pts[0], pts[-1]]
        return rdp(pts[:idx + 1])[:-1] + rdp(pts[idx:])

    return rdp(list(points))


def _color(annot):
    c = (annot.colors or {}).get("stroke")
    if not c:
        c = (annot.colors or {}).get("fill")
    if not c:
        return Color(0.0, 0.0, 0.0, 1.0)
    alpha = annot.opacity
    alpha = 1.0 if alpha is None or alpha < 0 else float(alpha)
    if len(c) == 1:
        return Color(c[0], c[0], c[0], alpha)
    return Color(c[0], c[1], c[2], alpha)


def extract(path, tolerance=0.25, max_pages=None):
    """-> InkDocument in PDF points, y down."""
    if fitz is None:
        raise ImportError("PDF extraction needs PyMuPDF:  ./venv/bin/pip install pymupdf")
    doc = fitz.open(path)
    out = InkDocument(title=path)
    for pno, page in enumerate(doc):
        if max_pages is not None and pno >= max_pages:
            break
        ipage = InkPage(width=page.rect.width, height=page.rect.height,
                        source_id=f"page{pno}")
        for i, annot in enumerate(page.annots() or []):
            if annot.type[1] != "Ink":
                continue
            width = (annot.border or {}).get("width") or 1.0
            color = _color(annot)
            kind = ("highlighter"
                    if width >= HIGHLIGHTER_MIN_WIDTH_PT or color.a <= HIGHLIGHTER_MAX_ALPHA
                    else "pen")
            for j, sub in enumerate(_vertices(annot)):
                pts = simplify(_dedupe(sub), tolerance)
                if len(pts) < 2:
                    continue
                ipage.add(InkStroke(points=pts, color=color, width=float(width),
                                    kind=kind, source_id=f"p{pno}.a{i}.s{j}"))
        out.add_page(ipage)
    doc.close()
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = extract(sys.argv[1])
    print(d)
    for p in d.pages:
        print(f"  page {p.source_id}  {p.width:.1f}x{p.height:.1f}pt  strokes={len(p)}")
        for s in p.strokes[:10]:
            x0, y0, x1, y1 = s.bounds
            print(f"    {s.source_id:>12}  {len(s.points):3d}pts  w={s.width:.3f}  "
                  f"{s.kind:11s} #{int(s.color.r*255):02x}{int(s.color.g*255):02x}"
                  f"{int(s.color.b*255):02x}  ({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f})")

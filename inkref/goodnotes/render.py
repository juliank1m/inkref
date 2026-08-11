"""Preview rendering: strokes -> SVG. Never part of the write path.

Paths come out in GoodNotes units; the viewBox is in units and the pixel size is scaled by
`POINTS_PER_UNIT`, so a preview is 1:1 with points without rewriting any coordinates.
"""
import re
import sys

from . import strokes as strokes_mod
from .document import Document, POINTS_PER_UNIT

NUMBER = re.compile(r"-?\d+\.?\d*")


def _paths(doc):
    for page in doc.pages:
        for rec in page.live:
            sig, members = rec.geometry
            d, w = strokes_mod.svg_path(sig, members)
            if d:
                yield page, rec, d, max(w, 0.6)


def items(doc, color=None, opacity=1.0):
    """-> [(d, width, rgba, opacity)] ready for svg(). `color` overrides the ink colour."""
    return [(d, w, color or rec.color or (0, 0, 0, 1), opacity)
            for _, rec, d, w in _paths(doc)]


def bbox(layers, pad=30.0):
    nums = [float(x) for _, its in layers for d, *_ in its for x in NUMBER.findall(d)]
    if not nums:
        raise ValueError("nothing drawable")
    xs, ys = nums[0::2], nums[1::2]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def svg(layers, pad=30.0, scale=POINTS_PER_UNIT, box=None, extra="", background="white"):
    """layers: [(label, [(d, width, rgba, opacity)])]. `box` pins the viewBox so two
    renders can be overlaid; `extra` is raw SVG appended on top (structure overlays)."""
    x0, y0, x1, y1 = box or bbox(layers, pad)
    w, h = x1 - x0, y1 - y0
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w * scale:.0f}" '
           f'height="{h * scale:.0f}" viewBox="{x0:.2f} {y0:.2f} {w:.2f} {h:.2f}">']
    if background:
        out.append(f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{w:.2f}" height="{h:.2f}" '
                   f'fill="{background}"/>')
    for _, its in layers:
        for d, width, rgba, op in its:
            col = f"rgb({int(rgba[0] * 255)},{int(rgba[1] * 255)},{int(rgba[2] * 255)})"
            out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-opacity="{op}" '
                       f'stroke-width="{width:.2f}" stroke-linecap="round" '
                       f'stroke-linejoin="round"/>')
    if extra:
        out.append(extra)
    out.append("</svg>")
    return "\n".join(out)


def to_png(svg_text, scale=1.5):
    """-> PNG bytes, or None when no rasteriser is installed.

    Only the vision path needs this, and that path is optional, so a missing PyMuPDF
    degrades to a text-only classification rather than an error.
    """
    try:
        import fitz
    except ImportError:
        return None
    try:
        doc = fitz.open(stream=svg_text.encode(), filetype="svg")
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(scale, scale))
        return pix.tobytes("png")
    except Exception:                       # any rasteriser failure is non-fatal here
        return None


def to_png_tiles(svg_text, rects, scale=5.0):
    """-> [PNG bytes] for each (x0, y0, x1, y1) clip, or None if no rasteriser.

    `rects` are in the SVG's own *declared pixel* space, which `svg()` sets to points. The
    document is parsed once: a page of ten thousand paths costs more to parse than to draw,
    and re-parsing it per tile is what made a first tiled pass take seconds per page.
    """
    try:
        import fitz
    except ImportError:
        return None
    try:
        page = fitz.open(stream=svg_text.encode(), filetype="svg")[0]
        m = fitz.Matrix(scale, scale)
        return [page.get_pixmap(matrix=m, clip=fitz.Rect(*r)).tobytes("png")
                for r in rects]
    except Exception:
        return None


def render_file(path, out_path):
    its = items(Document.open(path))
    if not its:
        raise SystemExit(f"{path}: nothing drawable")
    with open(out_path, "w") as fh:
        fh.write(svg([("main", its)]))
    return out_path


def render_comparison(before_path, after_path, out_path):
    """Original in faded grey, result in colour, same coordinate space."""
    old = items(Document.open(before_path), color=(0.6, 0.6, 0.6, 1), opacity=0.9)
    new = items(Document.open(after_path))
    with open(out_path, "w") as fh:
        fh.write(svg([("before", old), ("after", new)]))
    return out_path


if __name__ == "__main__":
    if len(sys.argv) == 4:
        print(render_comparison(sys.argv[1], sys.argv[2], sys.argv[3]))
    else:
        print(render_file(sys.argv[1], sys.argv[2]))

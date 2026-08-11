"""Seeing which stage is wrong, instead of guessing from the final result.

The pipeline has five places to be wrong — the render, the recogniser, the coordinate
transform, the mapping onto strokes, and the plan — and every one of them fails the same
way from the outside: the page comes out looking much like it went in. Reading numbers
does not separate them. Looking at the page does.

    python -m inkref debug notes.goodnotes --page 1 --out debug.png

Each layer answers one question:

    strokes    are the native stroke boxes where the ink is?         (render, parse)
    ocr-lines  did the recogniser find the writing, and in the       (recognition)
               right place?                                          (transform)
    ocr-words  did it split the line into words?
    groups     did the right original strokes get attached to        (mapping)
               each word? drawn from the STROKES, not the OCR box,
               so a green box in the wrong place is a mapping bug
    unmatched  what was left alone — expected on diagrams, a
               problem on prose
    blocks     were the columns separated?                            (statistics)
    baselines  where is each line now, and where will it be sent?     (plan)
"""
import argparse

from .goodnotes import beautify as bt
from .goodnotes import render
from .goodnotes.document import Document, UNITS_PER_POINT
from .ink import grouping, layout, recognize

LAYERS = ("strokes", "ocr-lines", "ocr-words", "groups", "unmatched", "blocks",
          "baselines")


def _rect(box, stroke, width=1.0, dash=None, fill="none", opacity=1.0):
    u = UNITS_PER_POINT
    x0, y0, x1, y1 = (v * u for v in box)
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{max(x1 - x0, 0.5):.1f}" '
            f'height="{max(y1 - y0, 0.5):.1f}" fill="{fill}" fill-opacity="{opacity}" '
            f'stroke="{stroke}" stroke-width="{width * u:.2f}"{d}/>')


def _hline(y, x0, x1, stroke, width=1.0, dash=None):
    u = UNITS_PER_POINT
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x0 * u:.1f}" y1="{y * u:.1f}" x2="{x1 * u:.1f}" '
            f'y2="{y * u:.1f}" stroke="{stroke}" stroke-width="{width * u:.2f}"{d}/>')


def overlay(boxes, lines=(), groups=(), unmatched=(), analysis=None, offsets=None,
            layers=LAYERS):
    """-> raw SVG for `render.svg(extra=...)`, in GoodNotes units."""
    u = UNITS_PER_POINT
    out = []
    if "strokes" in layers:
        for b in boxes:
            out.append(_rect(b, "#94a3b8", 0.15))
    if "unmatched" in layers and unmatched:
        for i in unmatched:
            out.append(_rect(boxes[i], "#3b82f6", 0.35, fill="#3b82f6", opacity=0.12))
    if "ocr-words" in layers:
        for line in lines:
            for w in line.words:
                out.append(_rect(w.box, "#f59e0b", 0.35, dash="6 4"))
    if "ocr-lines" in layers:
        for line in lines:
            out.append(_rect(line.box, "#ef4444", 0.6))
    if "groups" in layers:
        for g in groups:
            out.append(_rect(g.box, "#22c55e", 0.5, fill="#22c55e", opacity=0.10))
    if analysis is not None and "blocks" in layers:
        for x in analysis.columns:
            out.append(f'<line x1="{x * u:.1f}" y1="0" x2="{x * u:.1f}" y2="100000" '
                       f'stroke="#a855f7" stroke-width="{1.5 * u:.2f}" '
                       f'stroke-dasharray="10 6"/>')
        for x in analysis.levels:
            out.append(f'<line x1="{x * u:.1f}" y1="0" x2="{x * u:.1f}" y2="100000" '
                       f'stroke="#f472b6" stroke-width="{0.6 * u:.2f}" '
                       f'stroke-dasharray="4 8"/>')
    if analysis is not None and "baselines" in layers:
        for k, line in enumerate(analysis.lines):
            out.append(_hline(line.baseline, line.box[0], line.box[2], "#f59e0b", 0.5))
            # where the plan is sending it: the gap between the two IS the correction
            if offsets:
                dy = [offsets[i][1] for i in line.indices]
                if dy and any(dy):
                    shift = sorted(dy)[len(dy) // 2]
                    out.append(_hline(line.baseline + shift, line.box[0], line.box[2],
                                      "#e879f9", 0.5, dash="5 3"))
    return "\n".join(out)


def page_debug(page, reader=None, strength="balanced", layers=LAYERS, structure="ocr"):
    """-> (svg text, one-line summary) for a single page."""
    drawn = bt.page_strokes(page)
    boxes = [b for _, b, _, _ in drawn]
    if len(boxes) < 2:
        return None, f"{page.id[:8]}: too little ink"

    lines, groups, unmatched = [], [], []
    if structure == "ocr" and reader is not None:
        lines, groups, unmatched = bt.read_page(page, reader)
    if groups:
        a = grouping.analysis(groups, boxes)
        how = f"ocr: {grouping.summary(lines, groups, boxes)}"
    else:
        a = layout.analyze(boxes)
        unmatched = []
        how = f"geometry: {len(a.lines)} lines, {len(a.words)} words"

    offsets, used, hurt = layout.verified_plan(a, boxes, layout.strength(strength))
    moved = sum(1 for dx, dy in offsets if dx or dy)
    w, h = bt.page_size(page, boxes)
    svg = render.svg(
        [("ink", [(d, wd, (0.10, 0.10, 0.12, 1), 1.0) for _, _, d, wd in drawn])],
        box=(0.0, 0.0, w * UNITS_PER_POINT, h * UNITS_PER_POINT),
        extra=overlay(boxes, lines, groups, unmatched, a, offsets, layers))
    return svg, (f"{page.id[:8]}: {how} | ref_h {a.ref_h:.1f}pt pitch {a.pitch:.1f}pt "
                 f"blocks {len(a.blocks)} | {moved}/{len(boxes)} strokes moved"
                 + (f", declined ({hurt})" if hurt else
                    f", {used.name}" if used else ", no plan"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="inkref debug",
                                 description="Draw what each pipeline stage found.")
    ap.add_argument("document")
    ap.add_argument("--page", type=int, default=0, help="0-based page index")
    ap.add_argument("--out", default="debug.png", help=".png or .svg")
    ap.add_argument("--strength", default="balanced")
    ap.add_argument("--layers", default=",".join(LAYERS),
                    help=f"comma-separated subset of: {','.join(LAYERS)}")
    ap.add_argument("--structure", choices=("ocr", "geometry"), default="ocr")
    ap.add_argument("--scale", type=float, default=2.0, help="PNG pixels per point")
    ap.add_argument("--crop", help="x0,y0,x1,y1 in points — zoom in on one region")
    args = ap.parse_args(argv)

    doc = Document.open(args.document)
    page = doc.pages[args.page]
    reader = recognize.recognizer()
    if args.structure == "ocr" and reader is None:
        print("no text recogniser on this machine (needs pyobjc); using geometry")
    svg, note = page_debug(page, reader, args.strength,
                           tuple(args.layers.split(",")), args.structure)
    if svg is None:
        raise SystemExit(note)
    print(note)

    if args.out.endswith(".svg"):
        with open(args.out, "w") as fh:
            fh.write(svg)
    else:
        if args.crop:
            rect = [float(v) for v in args.crop.split(",")]
            png = (render.to_png_tiles(svg, [rect], scale=args.scale) or [None])[0]
        else:
            png = render.to_png(svg, scale=args.scale)
        if png is None:
            raise SystemExit("no rasteriser (pip install pymupdf); write a .svg instead")
        with open(args.out, "wb") as fh:
            fh.write(png)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

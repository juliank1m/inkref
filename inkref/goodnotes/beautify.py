"""Apply the layout plan to a real `.goodnotes`, by translating existing records.

This is the whole product in one idea: **nothing is re-authored.** Each stroke record
keeps its own geometry, colour, width, pressure, render outline, identity, paint order and
every protobuf field we never decoded — only its coordinates move. That is why the output
is still native editable ink, and it is the one edit confirmed in the app to survive
intact: FINDINGS milestone 1 moved a record +100 units and Goodnotes rendered it smooth,
lasso-selectable and erasable, with an unchanged bounding-box size.

It also sidesteps the writer's limits. Authoring is constant-width only, but *translating*
works on any family we can parse, so a page of variable-width Apple Pencil ink beautifies
without ever being converted.
"""
import os
from dataclasses import dataclass, field

from ..ink import grouping
from ..ink import layout
from ..ink import recognize
from ..ink import smooth as smoothing
from . import render
from . import strokes as strokes_mod
from .document import Document, POINTS_PER_UNIT, UNITS_PER_POINT


@dataclass
class PageReport:
    page_id: str
    strokes: int = 0
    lines: int = 0
    words: int = 0
    moved: int = 0
    max_shift: float = 0.0                    # points
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    analysis: object = None                   # layout.Analysis, for previews
    semantic: object = None                   # ai.SemanticResult, when a classifier ran
    strength_used: object = None              # may be gentler than asked, or None = declined
    declined: object = None                   # the measure that stopped the plan being kept
    faired: int = 0                           # strokes whose tremble was damped
    boxes: list = field(default_factory=list)     # points, before
    offsets: list = field(default_factory=list)
    structure: str = "geometry"               # "geometry" or "ocr" — who found the lines
    recognized: list = field(default_factory=list)   # RecognizedLine, for the debug overlay
    groups: list = field(default_factory=list)       # WordGroup
    unmatched: list = field(default_factory=list)    # stroke indices no group claimed

    @property
    def improvement(self):
        """Fractional drop per metric. Positive is better."""
        out = {}
        for k, b in self.before.items():
            a = self.after.get(k, 0.0)
            out[k] = 0.0 if b == 0 else (b - a) / b
        return out


@dataclass
class Report:
    strength: str = "balanced"
    pages: list = field(default_factory=list)

    @property
    def moved(self):
        return sum(p.moved for p in self.pages)

    @property
    def strokes(self):
        return sum(p.strokes for p in self.pages)

    def summary(self):
        out = [f"strength={self.strength}  strokes={self.strokes}  moved={self.moved}"]
        for p in self.pages:
            imp = p.improvement
            out.append(
                f"  page {p.page_id[:8]}  {p.strokes:4d} strokes  "
                f"{p.lines:2d} lines  {p.words:3d} words  "
                f"max shift {p.max_shift:5.1f}pt"
                + ("" if p.strength_used in (None, self.strength)
                   else f"  [eased to {p.strength_used}]")
                + ("" if not p.declined
                   else f"  [declined: would worsen {p.declined}]")
                + ("" if not p.faired else f"  [faired {p.faired} strokes]"))
            # after its own page header, not before it — printed first, this read as though
            # it belonged to the page above
            if p.semantic:
                labels = ", ".join(f"{n}x{r}" for r, n in sorted(
                    {r: p.semantic.roles.count(r) for r in set(p.semantic.roles)}.items()))
                out.append(f"        semantics via {p.semantic.source}: {labels}")
                for w in p.semantic.warnings:
                    out.append(f"        ! {w}")
            if p.before:
                out.append(
                    "        baseline {:.2f}->{:.2f}pt ({:+.0%})   "
                    "pitch {:.2f}->{:.2f}pt ({:+.0%})   "
                    "margin {:.2f}->{:.2f}pt ({:+.0%})   "
                    "word gap {:.2f}->{:.2f}pt ({:+.0%})".format(
                        p.before["baseline_spread"], p.after["baseline_spread"],
                        imp["baseline_spread"],
                        p.before["pitch_spread"], p.after["pitch_spread"],
                        imp["pitch_spread"],
                        p.before["margin_spread"], p.after["margin_spread"],
                        imp["margin_spread"],
                        p.before["gap_spread"], p.after["gap_spread"], imp["gap_spread"]))
        return "\n".join(out)


def page_strokes(page):
    """-> [(record, box_in_points, svg_d, width_units)] for every drawable live record.

    One filter, used by both the transform and the preview, so what a preview animates is
    exactly what gets written. Tombstones and emptied geometry drop out here.
    """
    out = []
    for rec in page.live:
        sig, members = rec.geometry
        box = strokes_mod.bounds(sig, members)
        d, width = strokes_mod.svg_path(sig, members)
        if box is None or not d:
            continue
        # width stays raw here. A minimum-visible-width floor belongs to whoever is
        # drawing, not to the data — and applying it in units rather than points is what
        # made the Swift and Python digests disagree on one stroke.
        out.append((rec, tuple(v * POINTS_PER_UNIT for v in box), d, width))
    return out


def page_boxes(page):
    """-> (records, boxes in points)."""
    drawn = page_strokes(page)
    return [r for r, _, _, _ in drawn], [b for _, b, _, _ in drawn]


def page_image(page, scale=1.5):
    """A PNG of the page for a vision model, or None if it cannot be rendered."""
    drawn = page_strokes(page)
    if not drawn:
        return None
    its = [(d, w, rec.color or (0, 0, 0, 1), 1.0) for rec, _, d, w in drawn]
    return render.to_png(render.svg([("ink", its)]), scale=scale)


def page_size(page, boxes=None):
    """The page rectangle in points, from the document if it declares one."""
    size = page.paper.get("size") if page.paper else None
    if size and size[0] > 1 and size[1] > 1:
        return tuple(size)
    boxes = boxes or [b for _, b, _, _ in page_strokes(page)]
    if not boxes:
        return (1.0, 1.0)
    # No declared paper. The ink's own extent is still measured from the page origin, so
    # the PageTransform invariant holds; only the outer edge is a guess.
    return (max(b[2] for b in boxes) * 1.02, max(b[3] for b in boxes) * 1.02)


# A recogniser normalises its input to a fixed working size, so what decides whether small
# writing survives is the height of a line **as a fraction of the image**, not its pixel
# height. That is why a whole page of dense notes reads badly at any scale and a slice of
# it reads well: measured on a real 595x842pt page of maths, one image gave 37% of strokes
# grouped and 24 tiles gave 86%, with the render scale making almost no difference.
#
# A tile aims to hold about this many lines of writing. Tuning knob: fewer means more,
# smaller tiles — better recall on cramped writing, more recogniser calls.
LINES_PER_TILE = 25
TILE_OVERLAP = 0.08     # of a tile, so a line on a seam is whole in at least one of them
RENDER_SCALE = 5.0      # pixels per point


def page_tiles(page, ref_h, scale=RENDER_SCALE, lines_per_tile=LINES_PER_TILE):
    """-> [(PNG bytes, PageTransform)] covering the page, or [].

    Each tile covers **exactly** its rectangle — no padding, no crop to the ink — which is
    what lets `PageTransform` convert a recogniser's normalised box with one multiply.

    Black ink on white, and no paper template: a printed grid is a wall of horizontal rules
    to a text recogniser, and the writer's pen colour has nothing to do with reading.
    `page_image` still renders faithfully for the vision model, which is looking at
    something else entirely.
    """
    drawn = page_strokes(page)
    if not drawn:
        return []
    w, h = page_size(page, [b for _, b, _, _ in drawn])
    its = [(d, wd, (0, 0, 0, 1), 1.0) for _, _, d, wd in drawn]
    svg = render.svg([("ink", its)], box=(0.0, 0.0, w * UNITS_PER_POINT, h * UNITS_PER_POINT),
                     scale=POINTS_PER_UNIT, background="white")

    span = max(lines_per_tile * max(ref_h, 1.0) * 1.6, 60.0)   # ~1.6 x ref_h per line
    nx, ny = max(1, round(w / span)), max(1, round(h / span))
    tw, th = w / nx, h / ny
    rects, transforms = [], []
    for iy in range(ny):
        for ix in range(nx):
            x0 = max(0.0, ix * tw - TILE_OVERLAP * tw)
            y0 = max(0.0, iy * th - TILE_OVERLAP * th)
            x1 = min(w, (ix + 1) * tw + TILE_OVERLAP * tw)
            y1 = min(h, (iy + 1) * th + TILE_OVERLAP * th)
            rects.append((x0, y0, x1, y1))
            transforms.append(recognize.PageTransform(width=x1 - x0, height=y1 - y0,
                                                      scale=scale, x0=x0, y0=y0))
    pngs = render.to_png_tiles(svg, rects, scale=scale)
    return list(zip(pngs, transforms)) if pngs else []


def read_page(page, reader, scale=RENDER_SCALE):
    """Render, recognise, and map the result back onto this page's own strokes.

    -> (recognised lines, [WordGroup], [unmatched stroke index]). Never raises: a page the
    recogniser cannot read comes back empty and the caller falls back to geometry.
    """
    _, boxes = page_boxes(page)
    if reader is None or not boxes:
        return [], [], list(range(len(boxes)))
    # Only to size the tiles. A rough writing height is all that is needed for that, and
    # taking it from geometry costs nothing since the caller has usually measured it.
    ref_h = layout._ref_height(boxes)
    lines = []
    try:
        for png, t in page_tiles(page, ref_h, scale):
            lines += reader.recognize(png, t)
    except Exception:                       # a recogniser failing is not a document error
        return [], [], list(range(len(boxes)))
    lines = recognize.merge_stacked(recognize.dedupe(lines))
    groups, unmatched = grouping.map_strokes(lines, boxes)
    return lines, groups, unmatched


def classify(page, analysis, analyzer, vision=False):
    """Run the optional semantic layer over one page. Never raises."""
    if analyzer is None or not analysis.lines:
        return None
    image = page_image(page) if vision else None
    return analyzer.analyze(layout.describe(analysis), image)


def fair_page(page, iterations=2):
    """Damp the tremble in every stroke this code can author. -> how many were faired.

    Constant-width only, and that is not a limitation here: it is the family GoodNotes
    itself uses for a real notebook, and the only one we can write back. Colour, width and
    every undecoded field are untouched; only the polyline changes, and its endpoints do
    not move, so nothing the layout engine measured is invalidated.

    Off by default. Unlike every other transform in this file it changes a letter's shape
    rather than its position, so it is the user's call, not ours.
    """
    done = 0
    for rec in page.live:
        sig, members = rec.geometry
        if sig not in (strokes_mod.CONSTANT_WIDTH, strokes_mod.CONSTANT_WIDTH_V1):
            continue
        pts = strokes_mod.on_curve_points(sig, members)
        if len(pts) < 4:
            continue
        faired = smoothing.fair(pts, iterations=iterations)
        if len(faired) < 2:
            continue
        width = strokes_mod.svg_path(sig, members)[1]
        rec.geometry = strokes_mod.Stroke(points=faired, width=width,
                                          family=sig).to_tpl()
        done += 1
    return done


def plan_skip(pr):
    """Corrections that must not run on this page. -> a set of transform names.

    Line spacing is switched off wherever the structure came from a recogniser, and this is
    the one place it is decided.

    The other three corrections move ink *within* a line — a word to its baseline, a line to
    its margin, a gap to the page's rhythm — so the worst they can do is a slightly odd
    line. Line spacing moves whole lines past each other, and it can only be safe if it
    knows where every piece of ink on the page is. It does not: a recogniser reports the
    writing it can read and says nothing about the 10-15% it skipped, and that ink does not
    move. Spacing the lines around it drove them into it, tearing a fraction off its bar
    and stacking two lines of prose on top of each other on a real page.

    Reinstating it needs the planner to treat unread ink as an obstacle, not a tighter
    threshold — that is the next stage of this pipeline, not a tuning pass. Until then a
    page keeps the line rhythm its writer gave it, which was never the worst thing about it.
    """
    return {"line"} if pr.structure == "ocr" else set()


def beautify_document(doc, strength="balanced", apply=True, analyzer=None, vision=False,
                      smooth=False, reader=None):
    """Mutate `doc` in memory. Returns a Report; with apply=False it only measures.

    `reader` is an optional `ink.recognize.TextRecognizer`. Given one, the page is read
    and the lines and words come from what it found; without one they are inferred from
    stroke geometry as before. Recognition is only ever used to *group* — it supplies no
    coordinate, and a page it cannot read falls straight back to geometry.
    """
    s = layout.strength(strength)
    report = Report(strength=s.name)
    for page in doc.pages:
        recs, boxes = page_boxes(page)
        pr = PageReport(page_id=page.id, strokes=len(boxes), boxes=boxes)
        report.pages.append(pr)
        if len(boxes) < 2:
            continue

        analysis = None
        if reader is not None:
            pr.recognized, pr.groups, pr.unmatched = read_page(page, reader)
            if pr.groups:
                analysis = grouping.analysis(pr.groups, boxes)
                pr.structure = "ocr"
        if analysis is None:
            pr.unmatched = []
            analysis = layout.analyze(boxes)
        pr.semantic = classify(page, analysis, analyzer, vision)
        roles = pr.semantic.roles if pr.semantic else None
        if pr.semantic and pr.semantic.groups:
            # what the model grouped becomes one rigid line, so the plan below cannot
            # reach inside an equation to re-space it
            analysis = layout.merge_groups(analysis, pr.semantic.groups)
            roles = None
        offsets, used, hurt = layout.verified_plan(analysis, boxes, s, roles,
                                                   skip=plan_skip(pr))
        pr.strength_used = used.name if used else None
        pr.declined = hurt
        pr.analysis = analysis
        pr.offsets = offsets
        pr.lines = len(analysis.lines)
        pr.words = len(analysis.words)
        roles = pr.semantic.roles if pr.semantic else None
        pr.before = layout.metrics(boxes, analysis, roles)
        # scored on the same lines, not a re-analysis of the result: otherwise structural
        # churn on a dense page reads as a regression no correction caused, and the
        # numbers shown disagree with the guard that accepted the plan
        shifted = layout.moved(boxes, offsets)
        pr.after = layout.metrics(shifted, layout.reproject(analysis, shifted), roles)
        pr.moved = sum(1 for dx, dy in offsets if dx or dy)
        pr.max_shift = max((max(abs(dx), abs(dy)) for dx, dy in offsets), default=0.0)

        if apply and smooth:
            # before the offsets, so the layout is planned on the geometry as written
            pr.faired = fair_page(page)
        if apply:
            for rec, (dx, dy) in zip(recs, offsets):
                if dx or dy:
                    rec.translate(dx * UNITS_PER_POINT, dy * UNITS_PER_POINT)
    return report


def beautify_file(in_path, out_path, strength="balanced", analyzer=None, vision=False,
                  smooth=False, reader=None):
    """Read `in_path`, write a beautified copy to `out_path`. The input is never touched.

    SPEC §15: transformations happen on a copy, and unknown document structures pass
    through unchanged — every record is held as raw bytes and only field-patched.
    """
    if os.path.abspath(in_path) == os.path.abspath(out_path):
        raise ValueError("refusing to overwrite the source document; pick another path")
    doc = Document.open(in_path)
    report = beautify_document(doc, strength, analyzer=analyzer, vision=vision,
                               smooth=smooth, reader=reader)
    doc.write(out_path)
    return report


def analyze_file(in_path, strength="balanced", analyzer=None, vision=False, reader=None):
    """Structure and metrics only — nothing is written."""
    return beautify_document(Document.open(in_path), strength, apply=False,
                             analyzer=analyzer, vision=vision, reader=reader)

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

from ..ink import layout
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


def beautify_document(doc, strength="balanced", apply=True, analyzer=None, vision=False,
                      smooth=False):
    """Mutate `doc` in memory. Returns a Report; with apply=False it only measures."""
    s = layout.strength(strength)
    report = Report(strength=s.name)
    for page in doc.pages:
        recs, boxes = page_boxes(page)
        pr = PageReport(page_id=page.id, strokes=len(boxes), boxes=boxes)
        report.pages.append(pr)
        if len(boxes) < 2:
            continue

        analysis = layout.analyze(boxes)
        pr.semantic = classify(page, analysis, analyzer, vision)
        roles = pr.semantic.roles if pr.semantic else None
        if pr.semantic and pr.semantic.groups:
            # what the model grouped becomes one rigid line, so the plan below cannot
            # reach inside an equation to re-space it
            analysis = layout.merge_groups(analysis, pr.semantic.groups)
            roles = None
        offsets, used, hurt = layout.verified_plan(analysis, boxes, s, roles)
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
                  smooth=False):
    """Read `in_path`, write a beautified copy to `out_path`. The input is never touched.

    SPEC §15: transformations happen on a copy, and unknown document structures pass
    through unchanged — every record is held as raw bytes and only field-patched.
    """
    if os.path.abspath(in_path) == os.path.abspath(out_path):
        raise ValueError("refusing to overwrite the source document; pick another path")
    doc = Document.open(in_path)
    report = beautify_document(doc, strength, analyzer=analyzer, vision=vision,
                               smooth=smooth)
    doc.write(out_path)
    return report


def analyze_file(in_path, strength="balanced", analyzer=None, vision=False):
    """Structure and metrics only — nothing is written."""
    return beautify_document(Document.open(in_path), strength, apply=False,
                             analyzer=analyzer, vision=vision)

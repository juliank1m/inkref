"""InkDocument -> .goodnotes.

The only place that knows both the intermediate representation and GoodNotes. Keeps
§24's separation: nothing upstream of here deals in GoodNotes units, tpl, or protobuf.

Current constraint (CONFIRMED): we clone an existing record for the container fields whose
semantics are not established, so writing requires a template archive that already has at
least one live stroke on each target page. Building a document with no template is untested.
"""
from .document import Document, UNITS_PER_POINT
from .strokes import Stroke, CONSTANT_WIDTH


class TemplateError(RuntimeError):
    pass


# Coordinates scale by 11/6 — CONFIRMED exactly, three independent ways (FINDINGS §2) and
# again by round-tripping through GoodNotes' own PDF export, where an authored y=100 units
# came back as y=54.5454 pt.
#
# Stroke WIDTH uses a different unit: 1/144 inch, exactly 2 units per point. CONFIRMED by
# calibration — ten constant-width strokes from 0.5 to 24 units all exported at exactly
# units/2 points, spread 0.0000 over a 48x range. Do not "fix" this to 11/6; that is the
# coordinate scale and is 9% wrong for widths.
WIDTH_UNITS_PER_POINT = 2.0


def _to_units(points):
    return [(x * UNITS_PER_POINT, y * UNITS_PER_POINT) for x, y in points]


class GoodNotesWriter:
    """Writes an InkDocument into a copy of a template .goodnotes archive."""

    def __init__(self, template_path):
        self.template_path = template_path

    def write(self, ink_doc, out_path, clear_existing=False):
        doc = Document.open(self.template_path)
        targets = [p for p in doc.pages if p.live]
        if not targets:
            raise TemplateError(
                f"{self.template_path}: no page has a live stroke to use as a structural "
                f"template")
        if len(ink_doc.pages) > len(targets):
            raise TemplateError(
                f"InkDocument has {len(ink_doc.pages)} pages but the template offers only "
                f"{len(targets)} usable page(s). Multi-page output needs a bigger template.")

        written = 0
        for ink_page, page in zip(ink_doc.pages, targets):
            pending = list(ink_page.strokes)
            if clear_existing:
                # A template record has to survive to clone container fields from, so
                # rather than leaving a stray stroke on the page, the template *becomes*
                # the first output stroke: overwriting geometry and colour is a proven
                # edit, whereas authoring a tombstone to hide it is not. Later strokes
                # then clone the rewritten template.
                template = page.live[0]
                page.entries = [e for e in page.entries if e is template]
                if pending:
                    gn = self._convert(pending.pop(0))
                    template.geometry = gn.to_tpl()
                    template.color = gn.color
                    written += 1
            for stroke in pending:
                doc.add_stroke(page.id, self._convert(stroke))
                written += 1
        doc.write(out_path)
        return out_path, written

    @staticmethod
    def _convert(ink_stroke):
        return Stroke(
            points=_to_units(ink_stroke.points),
            color=ink_stroke.color.as_tuple(),
            width=ink_stroke.width * WIDTH_UNITS_PER_POINT,
            family=CONSTANT_WIDTH,
        )


def write(ink_doc, template_path, out_path, clear_existing=False):
    return GoodNotesWriter(template_path).write(ink_doc, out_path, clear_existing)


def reader_to_ink(path):
    """.goodnotes -> InkDocument. Lossy: geometry, colour and nominal width only.

    Useful for round-trip testing and, later, for feeding GoodNotes notebooks into the
    same transformation pipeline as PDFs.
    """
    from ..ink.model import Color, InkDocument, InkPage, InkStroke
    from . import strokes as strokes_mod
    from .document import POINTS_PER_UNIT

    doc = Document.open(path)
    out = InkDocument(title=path)
    for page in doc.pages:
        ipage = InkPage(source_id=page.id)
        for rec in page.live:
            sig, members = rec.geometry
            pts = strokes_mod.on_curve_points(sig, members)
            if len(pts) < 2:
                continue
            _, width = strokes_mod.svg_path(sig, members)
            r, g, b, a = rec.color or (0.0, 0.0, 0.0, 1.0)
            ipage.add(InkStroke(
                points=[(x * POINTS_PER_UNIT, y * POINTS_PER_UNIT) for x, y in pts],
                color=Color(r, g, b, a),
                width=width / WIDTH_UNITS_PER_POINT,
                source_id=rec.uuid,
            ))
        out.add_page(ipage)
    return out

"""The intermediate representation.

Everything converges here. Source parsers (PDF, later raster, later GoodNotes itself)
produce an InkDocument; transformations operate on one; target serializers consume one.
Nothing in this module may import a parser or a serializer.

Coordinates are **PDF points** (1/72 inch), origin top-left, y increasing downward.

That choice is deliberate:
  - PDF is the primary source format, so extraction needs no conversion
  - screen/image sources are also naturally top-left, y-down
  - GoodNotes' 1/132 inch unit is a serialization detail, converted in its writer only

PDF's own user space is y-UP from the bottom-left, so a PDF parser must flip; that flip
belongs in the parser, not here.
"""
from dataclasses import dataclass, field
from typing import Optional

PT_PER_INCH = 72.0


@dataclass(frozen=True)
class Color:
    """Straight (non-premultiplied) RGBA, each channel 0..1."""
    r: float = 0.0
    g: float = 0.0
    b: float = 0.0
    a: float = 1.0

    @classmethod
    def gray(cls, v, a=1.0):
        return cls(v, v, v, a)

    @classmethod
    def from_hex(cls, s, a=1.0):
        s = s.lstrip("#")
        return cls(int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255, a)

    def as_tuple(self):
        return (self.r, self.g, self.b, self.a)


@dataclass
class InkStroke:
    """One pen stroke: a centerline polyline plus how it is drawn.

    points: [(x, y)] in points, in drawing order. At least 2.
    width:  nominal stroke width in points. Constant for now; per-point widths are a
            later extension and are why `widths` exists as an optional parallel array.
    """
    points: list
    color: Color = field(default_factory=Color)
    width: float = 1.0
    widths: Optional[list] = None      # per-point widths, when a source provides them
    kind: str = "pen"                  # "pen" | "highlighter" — affects blending
    source_id: Optional[str] = None    # provenance: e.g. the PDF path index it came from

    def __post_init__(self):
        if len(self.points) < 2:
            raise ValueError("a stroke needs at least 2 points")
        if self.widths is not None and len(self.widths) != len(self.points):
            raise ValueError("widths must be parallel to points")

    @property
    def bounds(self):
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    def translated(self, dx, dy):
        return self._replace_points([(x + dx, y + dy) for x, y in self.points])

    def scaled(self, sx, sy=None, origin=(0.0, 0.0)):
        sy = sx if sy is None else sy
        ox, oy = origin
        pts = [(ox + (x - ox) * sx, oy + (y - oy) * sy) for x, y in self.points]
        s = self._replace_points(pts)
        s.width = self.width * (abs(sx) + abs(sy)) / 2
        if s.widths:
            s.widths = [w * (abs(sx) + abs(sy)) / 2 for w in s.widths]
        return s

    def _replace_points(self, pts):
        return InkStroke(points=pts, color=self.color, width=self.width,
                         widths=list(self.widths) if self.widths else None,
                         kind=self.kind, source_id=self.source_id)


@dataclass
class InkPage:
    """One page. width/height in points; None means "infer from content"."""
    strokes: list = field(default_factory=list)
    width: Optional[float] = None
    height: Optional[float] = None
    source_id: Optional[str] = None

    def add(self, stroke):
        self.strokes.append(stroke)
        return stroke

    @property
    def bounds(self):
        if not self.strokes:
            return None
        bs = [s.bounds for s in self.strokes]
        return (min(b[0] for b in bs), min(b[1] for b in bs),
                max(b[2] for b in bs), max(b[3] for b in bs))

    def __len__(self):
        return len(self.strokes)


@dataclass
class InkDocument:
    pages: list = field(default_factory=list)
    title: Optional[str] = None

    def add_page(self, page=None, **kw):
        page = page if page is not None else InkPage(**kw)
        self.pages.append(page)
        return page

    @property
    def stroke_count(self):
        return sum(len(p) for p in self.pages)

    def __repr__(self):
        return (f"<InkDocument {self.title or ''!r} pages={len(self.pages)} "
                f"strokes={self.stroke_count}>")

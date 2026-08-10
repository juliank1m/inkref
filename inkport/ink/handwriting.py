"""Synthetic handwriting, for fixtures and the demo.

There is no messy handwritten `.goodnotes` in `samples/` — the public archives hold at
most five strokes — so tests and the demo need a page they can generate. This makes one:
a single-stroke capital font laid out with deliberate, seeded defects of exactly the kinds
the layout engine is meant to fix (SPEC §9 step 1).

Output is an `InkDocument` in PDF points, y down, like every other source adapter. It is a
fixture generator, not a handwriting synthesiser — SPEC §16 rules that out, and nothing
downstream ever regenerates a letter.
"""
import random
from dataclasses import dataclass

from .model import Color, InkDocument, InkPage, InkStroke

# One entry per glyph: a list of pen strokes, each a polyline in a 0..1 x 0..1 box, y down.
# Deliberately one stroke per pen-down, so grouping has to be discovered from geometry.
GLYPHS = {
    "A": [[(0, 1), (.5, 0), (1, 1)], [(.2, .6), (.8, .6)]],
    "B": [[(0, 0), (0, 1)], [(0, 0), (.6, 0), (.8, .25), (.6, .5), (0, .5)],
          [(.6, .5), (.85, .75), (.6, 1), (0, 1)]],
    "C": [[(1, .2), (.6, 0), (.2, .15), (0, .5), (.2, .85), (.6, 1), (1, .8)]],
    "D": [[(0, 0), (0, 1)], [(0, 0), (.6, .05), (.9, .5), (.6, .95), (0, 1)]],
    "E": [[(1, 0), (0, 0), (0, 1), (1, 1)], [(0, .5), (.7, .5)]],
    "F": [[(1, 0), (0, 0), (0, 1)], [(0, .5), (.7, .5)]],
    "G": [[(1, .2), (.6, 0), (.2, .15), (0, .5), (.2, .85), (.6, 1), (1, .8), (1, .55),
           (.55, .55)]],
    "H": [[(0, 0), (0, 1)], [(1, 0), (1, 1)], [(0, .5), (1, .5)]],
    # serifs on purpose: a bare vertical bar is ~0 wide, and a zero-width glyph fakes a
    # word gap on both sides of itself, which is a fixture artefact, not a real defect
    "I": [[(.2, 0), (.8, 0)], [(.5, 0), (.5, 1)], [(.2, 1), (.8, 1)]],
    "J": [[(.3, 0), (1, 0)], [(.75, 0), (.75, .75), (.45, 1), (.1, .85)]],
    "K": [[(0, 0), (0, 1)], [(.95, 0), (.05, .55)], [(.35, .42), (1, 1)]],
    "L": [[(0, 0), (0, 1), (1, 1)]],
    "M": [[(0, 1), (.05, 0), (.5, .65), (.95, 0), (1, 1)]],
    "N": [[(0, 1), (0, 0), (1, 1), (1, 0)]],
    "O": [[(.5, 0), (.15, .2), (0, .5), (.15, .8), (.5, 1), (.85, .8), (1, .5), (.85, .2),
           (.5, 0)]],
    "P": [[(0, 1), (0, 0), (.7, 0), (.95, .28), (.7, .55), (0, .55)]],
    "Q": [[(.5, 0), (.15, .2), (0, .5), (.15, .8), (.5, 1), (.85, .8), (1, .5), (.85, .2),
           (.5, 0)], [(.62, .72), (1.05, 1.12)]],
    "R": [[(0, 1), (0, 0), (.7, 0), (.95, .28), (.7, .55), (0, .55)], [(.45, .55), (1, 1)]],
    "S": [[(1, .15), (.6, 0), (.2, .1), (.1, .35), (.5, .5), (.9, .62), (.85, .88), (.4, 1),
           (0, .85)]],
    "T": [[(0, 0), (1, 0)], [(.5, 0), (.5, 1)]],
    "U": [[(0, 0), (0, .7), (.3, 1), (.7, 1), (1, .7), (1, 0)]],
    "V": [[(0, 0), (.5, 1), (1, 0)]],
    "W": [[(0, 0), (.25, 1), (.5, .35), (.75, 1), (1, 0)]],
    "X": [[(0, 0), (1, 1)], [(1, 0), (0, 1)]],
    "Y": [[(0, 0), (.5, .5), (1, 0)], [(.5, .5), (.5, 1)]],
    "Z": [[(0, 0), (1, 0), (0, 1), (1, 1)]],
    "-": [[(.1, .55), (.9, .55)]],
    ".": [[(.42, .94), (.56, 1.0)]],
    ":": [[(.42, .3), (.56, .36)], [(.42, .92), (.56, .98)]],
    " ": [],
}

ADVANCE = 1.18          # glyph box plus letter spacing, in units of the cap height
WORD_SPACE = 0.90       # nominal extra space between words, same units


@dataclass
class Mess:
    """How badly the page is written. 0 is a perfectly ruled page.

    Each field is the half-range of a uniform jitter, in units of the cap height (or of
    the line pitch, for `pitch`). These are exactly the defects SPEC §8.4-§8.7 fix.
    """
    baseline: float = 0.20      # per-word vertical wobble
    spacing: float = 0.50       # per-gap horizontal jitter
    margin: float = 0.50        # per-line left-edge drift
    pitch: float = 0.30         # per-line vertical pitch jitter
    glyph: float = 0.02         # per-vertex tremor, never corrected — this is "identity"

    @classmethod
    def none(cls):
        return cls(0.0, 0.0, 0.0, 0.0, 0.0)


def _word(text, x, y, size, rnd, tremor):
    """-> (polylines, advance). y is the top of the cap height."""
    polys, cx = [], x
    for ch in text:
        for poly in GLYPHS.get(ch, GLYPHS[" "]):
            pts = []
            for px, py in poly:
                jx = rnd.uniform(-tremor, tremor) if tremor else 0.0
                jy = rnd.uniform(-tremor, tremor) if tremor else 0.0
                p = (cx + (px + jx) * size, y + (py + jy) * size)
                if not pts or abs(p[0] - pts[-1][0]) > 1e-6 or abs(p[1] - pts[-1][1]) > 1e-6:
                    pts.append(p)
            if len(pts) >= 2:
                polys.append(pts)
        cx += size * ADVANCE
    return polys, cx - x


def page(lines, *, size=22.0, page_w=595.0, page_h=842.0, left=80.0, top=110.0,
         pitch=None, indent=None, mess=Mess(), seed=7,
         color=None, width=1.5):
    """Lay out `lines` — a list of `(indent_level, text)` — as an InkPage.

    Blank text emits nothing but still consumes a line, which is how a section break
    survives into the output and gives the layout engine a paragraph gap to preserve.
    """
    rnd = random.Random(seed)
    pitch = pitch if pitch is not None else size * 2.0
    indent = indent if indent is not None else size * 2.2
    color = color if color is not None else Color.from_hex("1f2933")
    pg = InkPage(width=page_w, height=page_h)

    y, first = top, True
    for level, text in lines:
        if not first:
            y += pitch * (1 + rnd.uniform(-mess.pitch, mess.pitch))
        first = False
        if not text.strip():
            y += pitch * 0.9          # a section break, wide enough to read as deliberate
            continue
        x = left + level * indent + size * rnd.uniform(-mess.margin, mess.margin)
        for word in text.upper().split(" "):
            if not word:
                continue
            wy = y + size * rnd.uniform(-mess.baseline, mess.baseline)
            polys, adv = _word(word, x, wy, size, rnd, mess.glyph)
            for p in polys:
                pg.add(InkStroke(points=p, color=color, width=width,
                                 source_id=f"synthetic:{word}"))
            gap = size * (WORD_SPACE + rnd.uniform(-mess.spacing, mess.spacing))
            x += adv + max(gap, size * 0.25)
    return pg


LECTURE = [
    (0, "MACHINE LEARNING"),
    (0, ""),
    (0, "SUPERVISED LEARNING"),
    (1, "- CLASSIFICATION"),
    (2, "- NEURAL NETS"),
    (1, "- REGRESSION"),
    (0, ""),
    (0, "UNSUPERVISED LEARNING"),
    (1, "- CLUSTERING"),
    (1, "- EMBEDDINGS"),
]


def messy_notes(seed=7, mess=None, title="messy lecture notes", **kw):
    """The demo page: the SPEC §9 lecture notes, written badly on purpose."""
    doc = InkDocument(title=title)
    doc.add_page(page(LECTURE, mess=mess if mess is not None else Mess(), seed=seed, **kw))
    return doc

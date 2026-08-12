"""Build the notebook that ships with the app as "Try a sample".

Deliberately not a test fixture. The fixtures in `tests/` exist to make a specific rule
fire; this exists to look like something a student actually wrote, so that what the
formatter does to it is a fair demonstration rather than a staged one.

That constraint runs both ways. The page is messy in the ways handwriting is really messy
— a baseline that wanders, word gaps that vary, a left edge that drifts, a line pitch that
breathes — at amplitudes measured off a real notebook, not exaggerated to make the
before/after look dramatic. And it contains the two things the formatter must be seen NOT
to touch: a fraction whose numerator sits over its denominator, and a sketch. If those
came out changed the demo would be a lie, so they are in the sample on purpose.

    python3 tools/make_demo.py [out.goodnotes]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes.writer import GoodNotesWriter          # noqa: E402
from inkref.ink import handwriting as hw                     # noqa: E402
from inkref.ink.model import Color, InkDocument, InkStroke   # noqa: E402

TEMPLATE = os.path.join(ROOT, "samples", "test.goodnotes")
DEFAULT_OUT = os.path.join(ROOT, "ios", "InkRef", "Resources", "demo.goodnotes")

INK = Color.from_hex("1f2933")
BLUE = Color.from_hex("1d4ed8")

# Measured off a real page of lecture notes rather than chosen for effect: the baseline
# wanders about a fifth of the cap height, word gaps vary by half, the left edge drifts by
# half, and the line pitch breathes by a quarter. Enough that the page reads as hurried,
# little enough that it reads as somebody's actual notes.
MESS = hw.Mess(baseline=0.18, spacing=0.45, margin=0.55, pitch=0.26, glyph=0.03)

# One page, not three. The writer can only fill pages a template already has ink on, and
# the public template offers exactly one — so rather than ship a real notebook as a
# container, everything the demo has to show goes on a single sheet: a heading, prose, a
# bullet list, a stacked fraction and a sketch.
#
# Lines are kept under about twenty letters. At this cap height each glyph advances 1.18 of
# it and a word gap adds most of another, so a line of thirty runs past the right edge of
# an A4 page before the margin jitter is even applied — and ink hanging off the paper looks
# like a bug in the app rather than a property of the sample.
NOTES = [
    (0, "LIMITS AND CONTINUITY"),
    (0, ""),
    (0, "A LIMIT IS THE VALUE"),
    (0, "A FUNCTION APPROACHES"),
    (0, "NEAR A POINT. IT NEED"),
    (0, "NOT BE DEFINED THERE."),
    (0, ""),
    (0, "FOR CONTINUITY WE NEED"),
    (1, "- THE VALUE TO EXIST"),
    (1, "- THE LIMIT TO EXIST"),
    (1, "- THE TWO TO AGREE"),
    (0, ""),
    (0, "THE DERIVATIVE IS THE"),
    (0, "LIMIT OF THIS QUOTIENT"),
    (0, "AS H GOES TO ZERO"),
]


def stroke(page, points, color=INK, width=1.4):
    page.add(InkStroke(points=[(float(x), float(y)) for x, y in points],
                       color=color, width=width, source_id="demo"))


def fraction(page, x, y, size=15.0):
    """A difference quotient: numerator over a bar over a denominator, with an exponent.

    Written as separate pen strokes stacked vertically, which is exactly the shape the
    formatter has to recognise and refuse to re-space. If line spacing ever reached inside
    this, the numerator would drift off its bar and the demo would show it.
    """
    rnd = __import__("random").Random(21)
    for text, dy, scale in (("F(X+H) - F(X)", -1.25, 1.0), ("H", 0.95, 1.0)):
        polys, _ = hw._word(text.replace(" ", " "), x, y + dy * size, size * scale, rnd, 0.03)
        for p in polys:
            stroke(page, p)
    stroke(page, [(x - 3, y + 0.1 * size), (x + 11.5 * size, y + 0.1 * size)])   # the bar
    # a squared exponent, sitting above the line it belongs to
    polys, _ = hw._word("2", x + 4.15 * size, y - 1.7 * size, size * 0.55, rnd, 0.03)
    for p in polys:
        stroke(page, p)


def sketch(page, x, y, w=150.0, h=95.0):
    """Axes and a bell curve, drawn freehand. The one region on the page that geometry
    must leave alone: it has no baseline, no words and no margin to align to."""
    stroke(page, [(x, y - h), (x, y), (x + w, y)])                       # axes
    stroke(page, [(x - 4, y - h + 6), (x, y - h - 2), (x + 4, y - h + 6)])   # arrow head
    curve = []
    for i in range(41):
        t = i / 40
        px = x + 8 + t * (w - 16)
        py = y - 6 - (h - 26) * pow(2.718281828, -pow((t - 0.5) * 4.6, 2))
        curve.append((px, py))
    stroke(page, curve)
    for i in range(6):                                                   # hatching under it
        hx = x + 34 + i * 13
        stroke(page, [(hx, y - 4), (hx + 9, y - 30 - i % 3 * 5)])
    polys, _ = hw._word("Y", x - 13, y - h - 2, 11.0, __import__("random").Random(3), 0.03)
    for p in polys:
        stroke(page, p)
    polys, _ = hw._word("X", x + w + 4, y - 6, 11.0, __import__("random").Random(4), 0.03)
    for p in polys:
        stroke(page, p)


def build():
    doc = InkDocument(title="Calculus notes")
    page = hw.page(NOTES, size=15.0, left=64.0, top=88.0, pitch=30.0,
                   mess=MESS, seed=7, color=INK, width=1.4)
    # Both figures live below the prose, which now runs to about y=610. A fraction sitting
    # under a line of text is not a demonstration of anything except two pieces of ink in
    # the same place.
    fraction(page, 96.0, 700.0)
    sketch(page, 330.0, 800.0, w=210.0, h=120.0)
    # A margin note in a second colour. Every real page has one, and it is also the check
    # that colour survives: if the writer ever re-authored a stroke this would come back
    # black.
    # Out to the right of the bullet list, where nothing else is. Written over a line of
    # prose it reads as the formatter having collided two pieces of ink, which is the exact
    # accusation this page exists to disprove.
    polys, _ = hw._word("CHECK", 470.0, 320.0, 11.0, __import__("random").Random(9), 0.04)
    for p in polys:
        stroke(page, p, color=BLUE, width=1.2)
    doc.add_page(page)
    return doc


def main(argv):
    out = argv[1] if len(argv) > 1 else DEFAULT_OUT
    doc = build()
    GoodNotesWriter(TEMPLATE).write(doc, out, clear_existing=True)
    print(f"{out}  {len(doc.pages)} pages, "
          f"{sum(len(p.strokes) for p in doc.pages)} strokes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Milestone 4 — stress the writer before scaling (handoff §21).

Builds an InkDocument of N strokes across several colours and widths, writes it through
the GoodNotes writer, then re-reads and checks every invariant that has bitten us so far.

Run:  python3 milestones/m4_stress.py [count]
"""
import math
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkport.goodnotes import records, strokes as strokes_mod   # noqa: E402
from inkport.goodnotes.document import Document                 # noqa: E402
from inkport.goodnotes.writer import GoodNotesWriter            # noqa: E402
from inkport.ink.model import Color, InkDocument, InkPage, InkStroke  # noqa: E402

TEMPLATE = os.path.join(ROOT, "samples", "test.goodnotes")
OUT_DIR = os.path.join(ROOT, "generated")

# A4 in points; keep a margin so nothing lands off-page
PAGE_W, PAGE_H = 595.0, 842.0
PALETTE = [Color.from_hex("1a1a1a"), Color.from_hex("d62828"), Color.from_hex("1d4ed8"),
           Color.from_hex("0f9d58"), Color.from_hex("f59e0b"), Color.from_hex("7c3aed")]
WIDTHS = [0.6, 1.0, 1.6, 2.4, 3.6]


def build(count, cols=20):
    """A grid of small deterministic squiggles — many distinct strokes, modest points."""
    page = InkPage(width=PAGE_W, height=PAGE_H)
    rows = math.ceil(count / cols)
    cw = (PAGE_W - 60) / cols
    ch = (PAGE_H - 60) / max(rows, 1)
    for i in range(count):
        cx = 30 + (i % cols) * cw
        cy = 30 + (i // cols) * ch
        pts = [(cx + t * cw * 0.8 / 7, cy + math.sin(t * 0.9 + i) * ch * 0.3)
               for t in range(8)]
        page.add(InkStroke(points=pts, color=PALETTE[i % len(PALETTE)],
                           width=WIDTHS[i % len(WIDTHS)], source_id=f"s{i}"))
    doc = InkDocument(title=f"stress-{count}")
    doc.add_page(page)
    return doc


def verify(path, expected_new):
    doc = Document.open(path)
    page = next(p for p in doc.pages if p.live)
    recs = page.records
    live = page.live

    uuids = [r.uuid for r in recs]
    assert len(set(uuids)) == len(uuids), "UUID collision"
    versions = [r.version for r in recs]
    assert len(set(versions)) == len(versions), "version-stamp collision"
    orders = [r.order for r in recs if r.order is not None]
    assert len(set(orders)) == len(orders), "paint-order collision"

    for r in recs:
        assert r.is_consistent(), f"{r.uuid}: descriptor.f2 != item.f15"

    made = [r for r in live if r.geometry[0] == strokes_mod.CONSTANT_WIDTH]
    assert len(made) == expected_new, f"expected {expected_new} new strokes, got {len(made)}"
    for r in made:
        assert not r.deleted, f"{r.uuid}: born tombstoned"
        assert r.family_marker is None, f"{r.uuid}: constant-width must not carry field 3"
        b = strokes_mod.bounds(*r.geometry)
        assert b is not None, f"{r.uuid}: empty geometry"
    return len(recs), len(live)


def run(count):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"04_stress_{count}.goodnotes")

    t0 = time.perf_counter()
    ink = build(count)
    t1 = time.perf_counter()
    _, written = GoodNotesWriter(TEMPLATE).write(ink, out)
    t2 = time.perf_counter()
    total, live = verify(out, written)
    t3 = time.perf_counter()

    size = os.path.getsize(out)
    with zipfile.ZipFile(out) as z:
        page_member = max((z.getinfo(n).file_size, n) for n in z.namelist()
                          if n.startswith("notes/"))
    print(f"  {count:5d} strokes | build {t1-t0:5.2f}s  write {t2-t1:5.2f}s  "
          f"verify {t3-t2:5.2f}s | zip {size/1024:7.1f} KB  page member "
          f"{page_member[0]/1024:7.1f} KB | records {total} ({live} live)")
    return out


if __name__ == "__main__":
    counts = [int(a) for a in sys.argv[1:]] or [100, 1000]
    print(f"template: {os.path.relpath(TEMPLATE, ROOT)}")
    outs = [run(c) for c in counts]
    print("\nwrote:")
    for o in outs:
        print("  " + os.path.relpath(o, ROOT))
    print("\nAll writer-side invariants hold. Import to confirm rendering and "
          "interaction at scale.")

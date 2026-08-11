"""Milestone 2 — duplicate an existing stroke.

Clones the one drawable stroke record, gives the copy a fresh UUID, a fresh
{replica, clock} version and its own paint order, then offsets it so both are visible.
Establishes that we can ADD a native ink object, not merely mutate one.
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes import strokes as strokes_mod              # noqa: E402
from inkref.goodnotes.document import Document, to_points   # noqa: E402

SOURCE = os.path.join(ROOT, "samples", "test.goodnotes")
OUTPUT = os.path.join(ROOT, "generated", "02_duplicated_stroke.goodnotes")
DX, DY = 150.0, 0.0      # the stroke is only 35 units wide, so this clears it completely


def main():
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    doc = Document.open(SOURCE)
    page = next(p for p in doc.pages if p.records)
    original = next(r for r in page.live if strokes_mod.bounds(*r.geometry))
    src_index = page.live.index(original)   # duplicate_stroke indexes into live records

    before_uuid = original.uuid
    before_version = original.version
    before_order = original.order
    before_bounds = strokes_mod.bounds(*original.geometry)
    before_count = len(page.records)

    print(f"source   : {os.path.relpath(SOURCE, ROOT)}  schema={doc.schema}")
    print(f"page     : {page.id}")
    print(f"original : {before_uuid}  order={before_order} v={before_version}")
    print(f"           bounds={tuple(round(v, 2) for v in before_bounds)}")

    copy = doc.duplicate_stroke(page.id, src_index, dx=DX, dy=DY)
    doc.write(OUTPUT)

    # ---- verify ----
    check = Document.open(OUTPUT)
    out_page = check.pages[[p.id for p in check.pages].index(page.id)]
    assert len(out_page.records) == before_count + 1, "expected exactly one new record"

    drawable = [r for r in out_page.records if strokes_mod.bounds(*r.geometry)]
    assert len(drawable) == 2, f"expected 2 drawable strokes, got {len(drawable)}"

    kept = next(r for r in drawable if r.uuid == before_uuid)
    made = next(r for r in drawable if r.uuid != before_uuid)

    # the original must be completely untouched
    assert kept.version == before_version, "original version changed"
    assert kept.order == before_order, "original paint order changed"
    kb = strokes_mod.bounds(*kept.geometry)
    assert all(abs(a - b) < 1e-3 for a, b in zip(kb, before_bounds)), "original moved"

    # the copy must be a distinct, internally consistent object
    assert made.uuid != before_uuid, "uuid was reused"
    assert made.version != before_version, "version stamp was reused"
    assert made.order != before_order, "paint order was reused"
    assert made.version[1] > before_version[1], "clock must ascend"
    assert not made.deleted, "copy was born tombstoned"
    for rec in out_page.records:
        assert rec.is_consistent(), f"{rec.uuid}: descriptor/item mismatch"

    # same shape, offset by exactly the requested amount
    mb = strokes_mod.bounds(*made.geometry)
    for got, want, delta in ((mb[0], before_bounds[0], DX), (mb[1], before_bounds[1], DY),
                             (mb[2], before_bounds[2], DX), (mb[3], before_bounds[3], DY)):
        assert abs((got - want) - delta) < 1e-3, f"offset wrong: {got - want} != {delta}"
    assert abs((mb[2] - mb[0]) - (before_bounds[2] - before_bounds[0])) < 1e-3
    assert abs((mb[3] - mb[1]) - (before_bounds[3] - before_bounds[1])) < 1e-3

    # copy inherits colour, width and family
    assert made.color == kept.color
    assert made.geometry[0] == kept.geometry[0]

    # only the page member differs
    zin, zout = zipfile.ZipFile(SOURCE), zipfile.ZipFile(OUTPUT)
    assert zin.namelist() == zout.namelist()
    changed = [n for n in zin.namelist() if zin.read(n) != zout.read(n)]
    assert changed == [page.path], f"unexpected members changed: {changed}"

    print(f"copy     : {made.uuid}  order={made.order} v={made.version}")
    print(f"           bounds={tuple(round(v, 2) for v in mb)}")
    print(f"offset   : dx={DX} dy={DY} units ({to_points(DX):.2f} x {to_points(DY):.2f} pt)")
    print(f"strokes  : {before_count} -> {len(out_page.records)} records "
          f"({len(drawable)} drawable)")
    print(f"invariant: descriptor.f2 == item.f15 holds for all "
          f"{len(out_page.records)} records")
    print(f"original : untouched (same uuid, version, order, bounds)")
    print(f"zip      : only {page.path} differs")
    print(f"wrote    : {os.path.relpath(OUTPUT, ROOT)}")


if __name__ == "__main__":
    main()

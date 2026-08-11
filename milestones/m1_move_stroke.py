"""Milestone 1 — move an existing stroke.

Takes a minimal sample containing exactly one pen stroke, shifts its geometry by a
fixed amount, and rewrites the archive. Nothing else changes: same UUID, colour,
width, version, paint order, and every other ZIP member byte-identical.
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes import strokes as strokes_mod          # noqa: E402
from inkref.goodnotes.document import Document, to_points  # noqa: E402

# test2.goodnotes holds literally one stroke, but it is a zero-length dot 0.35 pt wide —
# useless for judging "did it move?" by eye. test.goodnotes has one *drawable* stroke
# (a 165 pt squiggle) plus one degenerate record with no geometry, which we leave alone.
SOURCE = os.path.join(ROOT, "samples", "test.goodnotes")
OUTPUT = os.path.join(ROOT, "generated", "01_moved_stroke.goodnotes")
DX, DY = 100.0, 0.0


def describe(rec):
    sig, members = rec.geometry
    return {
        "uuid": rec.uuid, "order": rec.order, "version": rec.version,
        "color": rec.color, "family": sig,
        "bounds": strokes_mod.bounds(sig, members),
        "members": [len(m) if isinstance(m, list) else m for m in members],
    }


def main():
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    doc = Document.open(SOURCE)
    pages = [p for p in doc.pages if p.records]
    assert len(pages) == 1, f"expected one page with strokes, got {len(pages)}"
    page = pages[0]

    drawable = [r for r in page.records
                if strokes_mod.bounds(*r.geometry) is not None]
    assert len(drawable) == 1, f"expected exactly one drawable stroke, got {len(drawable)}"
    empty = len(page.records) - 1

    rec = drawable[0]
    target_index = page.records.index(rec)
    before = describe(rec)
    print(f"source   : {os.path.relpath(SOURCE, ROOT)}  schema={doc.schema}")
    print(f"page     : {page.id}")
    print(f"family   : {before['family']}")
    print(f"before   : bounds={before['bounds']}  uuid={before['uuid']}")

    rec.translate(DX, DY)
    doc.write(OUTPUT)

    # ---- verify ----
    check = Document.open(OUTPUT)
    out_page = check.pages[[p.id for p in check.pages].index(page.id)]
    assert len(out_page.records) == len(page.records), "stroke count changed"
    got = out_page.records[target_index]
    after = describe(got)

    for key in ("uuid", "order", "version", "color", "family", "members"):
        assert before[key] == after[key], f"{key} changed: {before[key]} -> {after[key]}"

    bx0, by0, bx1, by1 = before["bounds"]
    ax0, ay0, ax1, ay1 = after["bounds"]
    for b, a, d in ((bx0, ax0, DX), (bx1, ax1, DX), (by0, ay0, DY), (by1, ay1, DY)):
        assert abs((a - b) - d) < 1e-3, f"expected shift {d}, got {a - b}"

    # every member except the rewritten page must be untouched
    zin, zout = zipfile.ZipFile(SOURCE), zipfile.ZipFile(OUTPUT)
    assert zin.namelist() == zout.namelist(), "member order changed"
    changed = [n for n in zin.namelist() if zin.read(n) != zout.read(n)]
    assert changed == [page.path], f"unexpected members changed: {changed}"

    print(f"after    : bounds={after['bounds']}")
    print(f"untouched: {empty} degenerate stroke record(s) with no geometry")
    print(f"shift    : dx={DX} dy={DY} units  ({to_points(DX):.2f} x {to_points(DY):.2f} pt)")
    print(f"unchanged: uuid, colour, width, version, order, family, member counts")
    print(f"zip      : only {page.path} differs; {len(zin.namelist()) - 1} members byte-identical")
    print(f"wrote    : {os.path.relpath(OUTPUT, ROOT)}")


if __name__ == "__main__":
    main()

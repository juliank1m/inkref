"""Diagnostic — which container field makes a constant-width stroke render?

Symptom: synthetic PConstantWidthStroke geometry imports into a schema-24 page with
byte-correct coordinates and lands in the `normal` bucket, but never draws. The page's
original PVariableWidthStroke draws fine.

Two container fields differ between an app-created constant-width record and the
variable-width record we clone as a template:

    item body field 3   = 1      present on variable-width, absent on constant-width
    descriptor field 14 = 5381   present on constant-width, absent on variable-width

The live library is 100% constant-width, so the two are confounded there. This writes one
archive containing five horizontal lines, identical except for those fields, at different
heights. Whichever lines render identifies the discriminator in a single import.

    y=100  red      A  control: exactly what failed (f3 kept, no f14)
    y=200  orange   B  item body field 3 removed
    y=300  green    C  descriptor field 14 = 5381 added
    y=400  blue     D  both B and C
    y=500  purple   E  positive control: whole container cloned from an app-created
                       constant-width record in the live library
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes import ids                       # noqa: E402
from inkref.goodnotes import protobuf as pb            # noqa: E402
from inkref.goodnotes import records                   # noqa: E402
from inkref.goodnotes import strokes as strokes_mod    # noqa: E402
from inkref.goodnotes.document import Document, Stroke   # noqa: E402

SOURCE = os.path.join(ROOT, "samples", "test.goodnotes")
OUTPUT = os.path.join(ROOT, "generated", "diag_family_fields.goodnotes")
LIVE_DB = os.path.expanduser(
    "~/Library/Containers/com.goodnotesapp.x/Data/Library/Databases/notes_main.db")

D_KIND = 14          # descriptor field seen only on app-created constant-width records
I_SHAPE = 3          # item body field seen only on variable-width records
KIND_CONST = 5381

VARIANTS = [
    ("A control  (f3 kept, no f14)", (0.85, 0.15, 0.15, 1.0), 100.0, False, False),
    ("B drop body f3",               (0.95, 0.55, 0.10, 1.0), 200.0, True,  False),
    ("C add desc f14=5381",          (0.10, 0.65, 0.25, 1.0), 300.0, False, True),
    ("D both",                       (0.10, 0.35, 0.90, 1.0), 400.0, True,  True),
]
E_COLOR, E_Y = (0.55, 0.15, 0.75, 1.0), 500.0


def live_const_template(db_path):
    """A descriptor/item pair from an app-created constant-width stroke, geometry stripped."""
    from rocksdict import Rdict, Options, AccessType
    db = Rdict(db_path, options=Options(raw_mode=True), access_type=AccessType.read_only())
    it = db.iter(); it.seek_to_first()
    while it.valid():
        k = it.key().decode("utf-8", "replace")
        if k.endswith(".item") and ".normal." in k:
            v = it.value()
            if records.item_type(v) == records.PEN_STROKE:
                d = db.get(k.replace(".item", ".descriptor").encode())
                if d:
                    rec = records.StrokeRecord(d, v)
                    if rec.geometry[0].startswith("vu"):
                        return rec
        it.next()
    return None


def line(y):
    return [(100.0, y), (200.0, y), (300.0, y), (400.0, y)]


def main():
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    doc = Document.open(SOURCE)
    page = next(p for p in doc.pages if p.live)
    template = page.live[0]

    made = []
    for label, color, y, drop_f3, add_f14 in VARIANTS:
        rec = template.clone(ids.new_uuid(), doc.versions.next(), doc._next_order(page))
        rec.geometry = Stroke(points=line(y), width=6.0).to_tpl()
        rec.color = color
        if drop_f3:
            rec._set_body(pb.patch(rec._body(), {I_SHAPE: None}))
        if add_f14:
            rec.descriptor = pb.upsert(rec.descriptor, D_KIND,
                                       pb.varint_field(D_KIND, KIND_CONST))
        page.append(rec)
        made.append((label, rec, y))

    if os.path.exists(LIVE_DB):
        src = live_const_template(LIVE_DB)
        if src is not None:
            rec = src.clone(ids.new_uuid(), doc.versions.next(), doc._next_order(page))
            rec.geometry = Stroke(points=line(E_Y), width=6.0).to_tpl()
            rec.color = E_COLOR
            page.append(rec)
            made.append(("E app-created container (positive control)", rec, E_Y))
        else:
            print("!! no app-created constant-width record found; skipping variant E")
    else:
        print("!! live library not present; skipping variant E")

    doc.write(OUTPUT)

    check = Document.open(OUTPUT)
    out_page = next(p for p in check.pages if p.id == page.id)
    by_uuid = {r.uuid: r for r in out_page.records}
    print(f"wrote {os.path.relpath(OUTPUT, ROOT)}  ({len(made)} probe strokes)\n")
    for label, rec, y in made:
        got = by_uuid[rec.uuid]
        body = pb.fields(got._body())
        desc = pb.fields(got.descriptor)
        sig, members = got.geometry
        assert sig == strokes_mod.CONSTANT_WIDTH
        assert not got.deleted
        print(f"  y={int(y):3d}  {label:44s} body_f3={body.get(I_SHAPE, '-')!s:>3}  "
              f"desc_f14={desc.get(D_KIND, '-')!s:>5}  bounds="
              f"{tuple(round(v) for v in strokes_mod.bounds(sig, members))}")
    print("\nImport once and report which coloured lines appear.")


if __name__ == "__main__":
    main()

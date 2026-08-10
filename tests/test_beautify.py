"""Document integrity: beautifying must never corrupt a .goodnotes file.

Run: python3 tests/test_beautify.py     (stdlib only)

SPEC §19 tests 1, 4 and 5 against the real transform — 2 and 3 are single-record edits and
stay in test_goodnotes.py. The fixture has to be generated: no sample archive holds a messy
page (the public ones carry at most five strokes), so handwriting.messy_notes() is written
into a copy of samples/test.goodnotes.

GoodNotesWriter(clear_existing=True) clears live strokes only, so the sample's own
tombstone comes through and is used as-is. No sample holds a non-pen-stroke item, though,
and content the engine cannot interpret surviving untouched is precisely what SPEC §19
test 5 is about — so one synthetic text-box pair is spliced in, or there is nothing to
prove.
"""
import hashlib
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkport.goodnotes import beautify as bt              # noqa: E402
from inkport.goodnotes import protobuf as pb              # noqa: E402
from inkport.goodnotes import records                     # noqa: E402
from inkport.goodnotes import strokes as strokes_mod      # noqa: E402
from inkport.goodnotes.document import Document           # noqa: E402
from inkport.goodnotes.writer import GoodNotesWriter      # noqa: E402
from inkport.ink import handwriting                       # noqa: E402

TEMPLATE = os.path.join(ROOT, "samples", "test.goodnotes")
GEN = os.path.join(ROOT, "generated")

# A text-box pair. Item field 8 is not PEN_STROKE, so document.py keeps the whole pair as
# opaque bytes and must hand them back verbatim.
FOREIGN = (pb.bytes_field(1, b"F0RE1GN0-0000-0000-0000-000000000001")
           + pb.varint_field(9, 90001),
           pb.bytes_field(records.TEXT_BOX, pb.varint_field(1, 42)))

_fixtures = {}      # name -> (path, sha256 at build time)
_runs = {}          # name -> (src, out, report, before, after)


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def fixture(name="messy", mess=None):
    if name not in _fixtures:
        path = os.path.join(GEN, f"_beautify_{name}.goodnotes")
        os.makedirs(GEN, exist_ok=True)
        GoodNotesWriter(TEMPLATE).write(
            handwriting.messy_notes(mess=mess), path, clear_existing=True)
        doc = Document.open(path)
        page = next(p for p in doc.pages if p.live)
        assert any(r.deleted for r in page.records), \
            "the writer dropped the template's tombstone"
        page.append(FOREIGN)
        doc.write(path)
        _fixtures[name] = (path, digest(path))
    return _fixtures[name][0]


def snapshot(path):
    """Everything a beautify must preserve, entry by entry in document order."""
    out = []
    for page in Document.open(path).pages:
        for e in page.entries:
            if not isinstance(e, records.StrokeRecord):
                out.append(("other", e))
                continue
            sig, members = e.geometry
            out.append(("stroke", {
                "uuid": e.uuid,
                "order": e.order,
                "color": e.color,
                "sig": sig,
                # scalars compare by value, arrays by element count: geometry may move
                # but must never be re-encoded into a different shape
                "shape": [len(m) if isinstance(m, list) else m for m in members],
                "bounds": strokes_mod.bounds(sig, members),
                "deleted": e.deleted,
                "marker": e.family_marker,
                "consistent": e.is_consistent(),
                "raw": (e.descriptor, e.item),
            }))
    return out


def run(name="messy", mess=None):
    """-> (src, out, report, before, after). Beautifies each fixture exactly once."""
    if name not in _runs:
        src = fixture(name, mess)
        out = os.path.join(GEN, f"_beautified_{name}.goodnotes")
        report = bt.beautify_file(src, out)
        _runs[name] = (src, out, report, snapshot(src), snapshot(out))
    return _runs[name]


def strokes_of(snap):
    return [r for kind, r in snap if kind == "stroke"]


def test_noop_rewrite_changes_nothing():
    """SPEC §19 test 1. A perfectly ruled page has an all-zero plan; the archive that
    comes back must be the same archive."""
    src, out, report, before, after = run("none", handwriting.Mess.none())
    assert report.moved == 0, f"a perfectly ruled page still moved {report.moved} strokes"
    a, b = zipfile.ZipFile(src), zipfile.ZipFile(out)
    assert a.namelist() == b.namelist(), "zip membership changed"
    # page content included: re-serializing untouched records is byte-exact
    # (test_goodnotes.test_identity_rewrite_is_byte_exact), so a zero plan has to be too
    for name in a.namelist():
        assert a.read(name) == b.read(name), f"{name} changed on a no-op beautify"
    assert before == after, "a zero plan still altered a record"
    print(f"  no-op: zero plan, {len(a.namelist())} members, nothing rewritten")


def test_stroke_population_is_identical():
    """SPEC §19 test 4: no stroke is created, dropped, reordered or recoloured."""
    _, _, report, before, after = run()
    b, a = strokes_of(before), strokes_of(after)
    assert len(a) == len(b) > 0, (len(b), len(a))
    assert {r["uuid"] for r in a} == {r["uuid"] for r in b}, "stroke identities changed"
    assert [r["order"] for r in a] == [r["order"] for r in b], "paint order changed"
    assert [r["color"] for r in a] == [r["color"] for r in b], "colour changed"
    print(f"  population: {len(a)} records, same uuids, same paint order, same colours")


def test_geometry_is_moved_never_re_encoded():
    """The tpl signature and every member's element count survive the transform."""
    _, _, _, before, after = run()
    for x, y in zip(strokes_of(before), strokes_of(after)):
        assert x["sig"] == y["sig"], f"{x['uuid']}: stroke family changed"
        assert x["shape"] == y["shape"], f"{x['uuid']}: tpl members re-encoded"
    print("  encoding: every tpl signature and member count unchanged")


def test_strokes_move_but_never_deform():
    """SPEC §7 preserve identity: a record may be translated, never reshaped."""
    _, _, _, before, after = run()
    moved = 0
    for x, y in zip(strokes_of(before), strokes_of(after)):
        if x["bounds"] is None:
            assert y["bounds"] is None, f"{x['uuid']}: empty geometry gained bounds"
            continue
        bx0, by0, bx1, by1 = x["bounds"]
        ax0, ay0, ax1, ay1 = y["bounds"]
        assert abs((ax1 - ax0) - (bx1 - bx0)) < 1e-3, f"{x['uuid']}: width changed"
        assert abs((ay1 - ay0) - (by1 - by0)) < 1e-3, f"{x['uuid']}: height changed"
        if abs(ax0 - bx0) > 1e-6 or abs(ay0 - by0) > 1e-6:
            moved += 1
    assert moved, "nothing moved — the fixture is not exercising the transform"
    print(f"  identity: {moved} records translated, every bbox size unchanged to 1e-3")


def test_every_emitted_record_is_valid():
    """The invariants that decide whether GoodNotes renders a stroke at all."""
    _, _, _, before, after = run()
    b, a = strokes_of(before), strokes_of(after)
    for r in a:
        assert r["consistent"], f"{r['uuid']}: descriptor/item uuid or version mismatch"
        assert r["marker"] == records.SHAPE_FOR_FAMILY[r["sig"]], \
            f"{r['uuid']}: family marker {r['marker']} disagrees with {r['sig']}"
    assert [r["deleted"] for r in a] == [r["deleted"] for r in b], \
        "the set of tombstoned records changed"
    print(f"  invariants: {len(a)} records consistent, markers in sync, no new tombstones")


def test_unsupported_content_passes_through_untouched():
    """SPEC §19 test 5. A tombstone and a non-pen entry are bytes we do not interpret."""
    _, _, _, before, after = run()
    dead = [r for r in strokes_of(before) if r["deleted"]]
    survived = [r for r in strokes_of(after) if r["deleted"]]
    assert len(dead) == 1, f"expected the spliced-in tombstone, got {len(dead)}"
    assert len(survived) == 1, f"the tombstone did not survive: {len(survived)} left"
    assert dead[0]["raw"] == survived[0]["raw"], "the tombstoned record was rewritten"
    foreign = [e for kind, e in before if kind == "other"]
    assert FOREIGN in foreign, "the fixture lost its non-pen entry"
    assert foreign == [e for kind, e in after if kind == "other"], \
        "a non-pen entry was rewritten"
    print("  passthrough: tombstone and text-box entry byte-identical after beautify")


def test_input_document_is_never_written_to():
    """SPEC §15: the transform happens on a copy."""
    src = fixture()
    want = _fixtures["messy"][1]
    bt.beautify_file(src, os.path.join(GEN, "_beautify_scratch.goodnotes"))
    assert digest(src) == want, "beautify_file modified its input"
    print("  source: input archive byte-identical before and after")


def test_beautify_refuses_to_overwrite_its_input():
    src = fixture()
    alias = os.path.join(GEN, os.pardir, "generated", os.path.basename(src))
    for target in (src, alias):
        try:
            bt.beautify_file(src, target)
        except ValueError:
            continue
        raise AssertionError(f"writing over the source via {target!r} should raise")
    print("  guard: in-place beautify refused, spelling of the path notwithstanding")


if __name__ == "__main__":
    for fn in [test_noop_rewrite_changes_nothing,
               test_stroke_population_is_identical,
               test_geometry_is_moved_never_re_encoded,
               test_strokes_move_but_never_deform,
               test_every_emitted_record_is_valid,
               test_unsupported_content_passes_through_untouched,
               test_input_document_is_never_written_to,
               test_beautify_refuses_to_overwrite_its_input]:
        print(fn.__name__)
        fn()
    for path in ([p for p, _ in _fixtures.values()]
                 + [r[1] for r in _runs.values()]
                 + [os.path.join(GEN, "_beautify_scratch.goodnotes")]):
        if os.path.exists(path):
            os.remove(path)
    print("\nall checks passed")

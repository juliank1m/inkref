"""Checks. Run: python3 test_gnre.py

Stdlib only — no GoodNotes install, no live library, no third-party packages.
"""
import glob
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes import ids                      # noqa: E402
from inkref.goodnotes import lz4                      # noqa: E402
from inkref.goodnotes import protobuf as pb           # noqa: E402
from inkref.goodnotes import records                  # noqa: E402
from inkref.goodnotes import strokes as strokes_mod   # noqa: E402
from inkref.goodnotes import tplfmt                   # noqa: E402
from inkref.goodnotes.document import Document, Stroke, to_points  # noqa: E402

SAMPLES = sorted(glob.glob(os.path.join(ROOT, "samples", "*.goodnotes")))


def test_protobuf_surgery():
    msg = pb.bytes_field(1, b"hello") + pb.varint_field(2, 300) + pb.f32_field(3, 1.5)
    assert [f for f, _, _ in pb.split(msg)] == [1, 2, 3]
    # unlisted fields must survive a patch byte-for-byte
    out = pb.patch(msg, {1: pb.bytes_field(1, b"world")})
    assert pb.split(out)[1][2] == pb.split(msg)[1][2]
    assert pb.split(out)[2][2] == pb.split(msg)[2][2]
    assert pb.fields(out)[1][0] == b"world"
    # length-delimited streams round-trip
    msgs = [b"", b"a", b"x" * 200]
    assert list(pb.read_stream(pb.write_stream(msgs))) == msgs
    print("  protobuf: split/patch preserve unknown fields; streams round-trip")


def test_tpl_roundtrip():
    sig = strokes_mod.CONSTANT_WIDTH
    pts = [(100.0 + 7 * i, 200.0 + (i % 3) * 5) for i in range(12)]
    sig, members = Stroke(points=pts, width=2.5).to_tpl()
    raw = tplfmt.dump(sig, members)
    got_sig, got = tplfmt.load(raw)
    assert got_sig == sig
    assert tplfmt.dump(got_sig, got) == raw, "tpl re-encode not byte-identical"
    back = strokes_mod.on_curve_points(got_sig, got)
    assert len(back) == len(pts)
    for (ax, ay), (bx, by) in zip(pts, back):
        assert abs(ax - bx) < 1e-3 and abs(ay - by) < 1e-3
    print("  tpl: byte-identical round-trip, geometry preserved")


def test_apple_lz4():
    for n in (1, 50, 5000):
        blob = bytes(range(256)) * n
        frame = lz4.compress(blob)
        assert lz4.is_framed(frame) and frame.endswith(lz4.TERMINATOR)
        assert lz4.decompress(frame) == blob
    print("  lz4: bv4 framing + round-trip via libcompression")


def test_units():
    for units, points in [((1122, 1452), (612, 792)),
                          ((1091.33935546875, 1543.46496582031), (595.28, 841.89)),
                          ((1452, 2244), (792, 1224))]:
        for u, p in zip(units, points):
            assert abs(to_points(u) - p) < 0.01, (u, to_points(u), p)
    print("  units: 1/132in -> pt lands on Letter / A4 / Legal exactly")


def test_color_roundtrip():
    """GoodNotes omits zero-valued fixed32 fields; encode/decode must agree."""
    for rgba in [(0.0, 0.0, 0.0, 1.0), (1.0, 0.0, 0.0, 1.0), (0.0, 0.5, 1.0, 0.25)]:
        got = records.decode_color(records.encode_color(rgba))
        assert all(abs(a - b) < 1e-6 for a, b in zip(rgba, got)), (rgba, got)
    assert records.encode_color((0.0, 0.0, 0.0, 1.0)).count(b"\x0d") == 0, \
        "zero channels must not be emitted"
    print("  colour: zero-valued channels omitted and recovered correctly")


def test_read_samples():
    assert SAMPLES, "no samples found"
    total = 0
    for path in SAMPLES:
        doc = Document.open(path)
        assert doc.schema in (24, 25)
        for page in doc.pages:
            for rec in page.records:
                total += 1
                sig, members = rec.geometry
                assert strokes_mod.is_supported(sig), sig
                assert len(rec.uuid) == 36
                assert len(rec.color) == 4
                if strokes_mod.bounds(sig, members) is not None:
                    d, w = strokes_mod.svg_path(sig, members)
                    assert d and d.startswith("M ")
    assert total > 0
    print(f"  read: {len(SAMPLES)} archives, {total} stroke records")


def test_identity_rewrite_is_byte_exact():
    """Opening and writing with no edits must reproduce the input exactly."""
    out = os.path.join(ROOT, "generated", "_identity.goodnotes")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    for path in SAMPLES:
        Document.open(path).write(out)
        a, b = zipfile.ZipFile(path), zipfile.ZipFile(out)
        assert a.namelist() == b.namelist(), path
        for name in a.namelist():
            assert a.read(name) == b.read(name), f"{path}:{name} changed on identity rewrite"
    os.remove(out)
    print(f"  identity: {len(SAMPLES)} archives rewrite byte-for-byte unchanged")


def test_translate_only_moves_geometry():
    path = os.path.join(ROOT, "samples", "test.goodnotes")
    doc = Document.open(path)
    page = next(p for p in doc.pages if p.records)
    rec = next(r for r in page.records if strokes_mod.bounds(*r.geometry))
    before_bounds = strokes_mod.bounds(*rec.geometry)
    before = (rec.uuid, rec.order, rec.version, rec.color)
    sig, members = rec.geometry
    shape = [len(m) if isinstance(m, list) else m for m in members]

    rec.translate(100.0, -25.0)

    sig2, members2 = rec.geometry
    assert sig2 == sig
    assert [len(m) if isinstance(m, list) else m for m in members2] == shape
    assert (rec.uuid, rec.order, rec.version, rec.color) == before
    x0, y0, x1, y1 = strokes_mod.bounds(sig2, members2)
    assert abs((x0 - before_bounds[0]) - 100.0) < 1e-3
    assert abs((y0 - before_bounds[1]) + 25.0) < 1e-3
    assert abs((x1 - x0) - (before_bounds[2] - before_bounds[0])) < 1e-3, "shape changed"
    print("  translate: geometry moves, identity/colour/shape unchanged")


def test_tombstones_are_detected_and_never_cloned():
    """Regression: test.goodnotes record 0 is a TOMBSTONE, not an empty stroke.

    Cloning it produced strokes that imported with byte-correct geometry but landed in
    Goodnotes' `deleted` bucket, so they never rendered. New records must never be born
    tombstoned, whatever they were cloned from.
    """
    path = os.path.join(ROOT, "samples", "test.goodnotes")
    doc = Document.open(path)
    page = next(p for p in doc.pages if p.records)

    dead = [r for r in page.records if r.deleted]
    assert len(dead) == 1, f"expected 1 tombstone in the fixture, got {len(dead)}"
    assert records.D_DELETED in pb.fields(dead[0].descriptor)
    assert len(page.live) == len(page.records) - 1
    assert all(not r.deleted for r in page.live)

    # cloning the tombstone directly must still yield a live record
    revived = dead[0].clone("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", (2, 999), 1)
    assert not revived.deleted, "retag must clear the tombstone markers"

    # and the public paths must not pick a tombstone as a template at all
    rec = doc.add_stroke(page.id, Stroke(points=[(10, 10), (20, 20)], width=2.0))
    assert not rec.deleted, "add_stroke produced a deleted record"
    copy = doc.duplicate_stroke(page.id, 0, dx=5)
    assert not copy.deleted, "duplicate_stroke produced a deleted record"
    print("  tombstones: detected, excluded as templates, cleared on retag")


def test_family_marker_tracks_geometry():
    """Item body field 3 must always agree with the tpl signature in the blob.

    Regression: writing constant-width geometry into a record still marked variable-width
    produced strokes that imported into `normal` with byte-correct coordinates and never
    rendered. Silent failure — no error anywhere.
    """
    path = os.path.join(ROOT, "samples", "test.goodnotes")
    doc = Document.open(path)
    page = next(p for p in doc.pages if p.live)

    src = page.live[0]
    assert src.geometry[0] == strokes_mod.VARIABLE_WIDTH
    assert src.family_marker == 1, "variable-width records must carry field 3 = 1"

    rec = doc.add_stroke(page.id, Stroke(points=[(10, 10), (60, 10)], width=3.0))
    assert rec.geometry[0] == strokes_mod.CONSTANT_WIDTH
    assert rec.family_marker is None, "constant-width must NOT carry field 3"

    # putting variable-width geometry back must restore the marker
    rec.geometry = src.geometry
    assert rec.family_marker == 1

    # and an unknown family must refuse rather than emit a silent dud
    try:
        rec.geometry = ("vvvv-not-a-real-family", [])
    except ValueError:
        pass
    else:
        raise AssertionError("unknown family should raise")
    print("  family marker: field 3 stays in sync with the tpl signature")


def test_version_allocator():
    alloc = ids.VersionAllocator.seeded_from([(2, 10), (2, 40), (2, 25)])
    a, b = alloc.next(), alloc.next()
    assert a == (2, 41) and b == (2, 42), (a, b)
    assert len(set(ids.new_uuid() for _ in range(50))) == 50
    print("  ids: clocks ascend past existing values, uuids unique")


if __name__ == "__main__":
    for fn in [test_protobuf_surgery, test_tpl_roundtrip, test_apple_lz4, test_units,
               test_color_roundtrip, test_version_allocator, test_read_samples,
               test_tombstones_are_detected_and_never_cloned,
               test_family_marker_tracks_geometry,
               test_identity_rewrite_is_byte_exact, test_translate_only_moves_geometry]:
        print(fn.__name__)
        fn()
    print("\nall checks passed")

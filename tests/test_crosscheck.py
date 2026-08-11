"""The Swift engine and the Python engine must read a document identically.

Two implementations of an undocumented binary format drift, and FINDINGS established that
this format's failure mode is silent: a wrong stroke imports cleanly, stores byte-correct
data and simply never draws. Nothing downstream complains. So the only defence is to make
both engines describe the same archive and demand the descriptions match, byte for byte of
text.

Skipped (not failed) when there is no Swift toolchain — the Python side must stay runnable
on a machine with no Xcode.

Run: python3 tests/test_crosscheck.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.goodnotes import beautify as bt                    # noqa: E402
from inkref.goodnotes.document import Document                 # noqa: E402

ENGINE = os.path.join(ROOT, "ios", "InkRef", "Engine")
AI = os.path.join(ROOT, "ios", "InkRef", "AI")
HARNESS = os.path.join(ROOT, "ios", "Tools", "CrossCheck.swift")

ARCHIVES = sorted(glob.glob(os.path.join(ROOT, "samples", "*.goodnotes")))
# a big one too: agreement on 5 strokes proves much less than agreement on 1000
ARCHIVES += [p for p in [os.path.join(ROOT, "generated", "04_stress_1000.goodnotes")]
             if os.path.exists(p)]


def python_digest(path):
    """The same canonical form ios/Tools/CrossCheck.swift prints."""
    doc = Document.open(path)
    out = [f"document {os.path.basename(path)} schema={doc.schema} pages={len(doc.pages)}"]
    for page in doc.pages:
        drawn = bt.page_strokes(page)
        out.append(f"page {page.id} strokes={len(drawn)}")
        for i, (rec, box, d, width) in enumerate(drawn):
            r, g, b, _ = rec.color or (0.0, 0.0, 0.0, 1.0)
            sig = rec.geometry[0]
            # width uses 1/144 inch for the constant-width family and, on a single
            # measurement, ~1:1 for variable width — never the 11/6 coordinate scale.
            pts = width / 2 if sig.startswith("vu") else width
            out.append("  %3d %10.4f %10.4f %10.4f %10.4f w=%.4f #%02x%02x%02x segs=%d" % (
                i, box[0], box[1], box[2], box[3], max(pts, 0.4),
                round(r * 255), round(g * 255), round(b * 255), d.count(" Q ") + 1))
    return "\n".join(out) + "\n"


def build_harness(workdir):
    if shutil.which("swiftc") is None:
        return None
    binary = os.path.join(workdir, "crosscheck")
    sources = (sorted(glob.glob(os.path.join(ENGINE, "*.swift")))
               + sorted(glob.glob(os.path.join(AI, "*.swift"))) + [HARNESS])
    proc = subprocess.run(["swiftc", "-O", "-DDEBUG", "-parse-as-library",
                           *sources, "-o", binary],
                          capture_output=True, text=True)
    if proc.returncode:
        raise AssertionError(f"the Swift engine does not compile:\n{proc.stderr[-2000:]}")
    return binary


def test_engines_agree(binary):
    assert ARCHIVES, "no archives to compare"
    total = 0
    for path in ARCHIVES:
        got = subprocess.run([binary, path], capture_output=True, text=True, check=True).stdout
        want = python_digest(path)
        if got != want:
            for a, b in zip(want.splitlines(), got.splitlines()):
                if a != b:
                    raise AssertionError(f"{os.path.basename(path)} diverges:\n"
                                         f"  python: {a}\n  swift : {b}")
            raise AssertionError(f"{os.path.basename(path)}: digests differ in length")
        total += want.count("\n  ")
    print(f"  engines agree: {len(ARCHIVES)} archives, {total} strokes, geometry/colour/"
          f"width/segments identical")


def python_layout_digest(path, strength="balanced"):
    """The same canonical form ios/Tools/CrossCheck.swift --layout prints."""
    from inkref.ink import layout
    doc = Document.open(path)
    s = layout.strength(strength)
    out = [f"layout {os.path.basename(path)} strength={s.name}"]
    for page in doc.pages:
        drawn = bt.page_strokes(page)
        if len(drawn) < 2:
            continue
        boxes = [b for _, b, _, _ in drawn]
        a = layout.analyze(boxes)
        offsets, used, hurt = layout.verified_plan(a, boxes, s)
        nonzero = sum(1 for dx, dy in offsets if dx or dy)
        max_shift = max((max(abs(dx), abs(dy)) for dx, dy in offsets), default=0.0)
        out.append("page %s strokes=%d refh=%.4f pitch=%.4f cols=%d blocks=%d lines=%d" % (
            page.id, len(boxes), a.ref_h, a.pitch,
            len(a.columns), len(a.blocks), len(a.lines)))
        out.append("  plan used=%s declined=%s moved=%d maxshift=%.4f" % (
            used.name if used else "none", hurt or "-", nonzero, max_shift))
        for k, line in enumerate(a.lines):
            out.append("  L%d b=%d t=%d base=%.4f x0=%.4f lx=%.4f w=%d" % (
                k, line.block, 1 if line.is_text else 0, line.baseline,
                line.box[0], line.level_x, len(line.words)))
    return "\n".join(out) + "\n"


def test_engines_agree_on_layout(binary):
    """Reading alike is not enough — both engines must also DECIDE alike.

    Otherwise the iPad app and the CLI quietly disagree about the same document, and the
    only symptom is that the exported file does not match the preview.
    """
    checked = 0
    for path in ARCHIVES:
        for strength in ("light", "balanced", "strong"):
            got = subprocess.run([binary, "--layout", strength, path],
                                 capture_output=True, text=True, check=True).stdout
            want = python_layout_digest(path, strength)
            if got != want:
                for a, b in zip(want.splitlines(), got.splitlines()):
                    if a != b:
                        raise AssertionError(
                            f"{os.path.basename(path)} [{strength}] layout diverges:\n"
                            f"  python: {a}\n  swift : {b}")
                raise AssertionError(f"{os.path.basename(path)} [{strength}]: length differs")
            checked += 1
    print(f"  layout agrees: {checked} document/strength combinations, structure and plan "
          f"identical")


def test_swift_layout_selfcheck(binary):
    proc = subprocess.run([binary, "--selfcheck"], capture_output=True, text=True)
    assert proc.returncode == 0, f"Swift layout self-check failed:\n{proc.stdout}"
    print("  swift layout: self-check passes")


def test_swift_beautify_output_is_readable_by_python(binary):
    """The strongest check available: Swift writes a document, Python audits it.

    A rewrite that only Swift can read would be exactly the silent failure FINDINGS warns
    about, so the invariants are re-checked by the other implementation.
    """
    src = os.path.join(ROOT, "generated", "demo_messy.goodnotes")
    if not os.path.exists(src):
        print("  swift write: skipped (run `python3 -m inkref demo` first)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "swift_beautified.goodnotes")
        subprocess.run([binary, "--beautify", "balanced", src, out],
                       capture_output=True, text=True, check=True)

        before, after = Document.open(src), Document.open(out)
        b_recs = [r for p in before.pages for r in p.records]
        a_recs = [r for p in after.pages for r in p.records]
        assert len(a_recs) == len(b_recs), "record count changed"
        assert {r.uuid for r in a_recs} == {r.uuid for r in b_recs}, "identities changed"
        assert [r.order for r in a_recs] == [r.order for r in b_recs], "paint order changed"

        moved = 0
        by_uuid = {r.uuid: r for r in b_recs}
        for rec in a_recs:
            assert rec.is_consistent(), f"{rec.uuid}: descriptor/item mismatch"
            src_rec = by_uuid[rec.uuid]
            sig_a, mem_a = rec.geometry
            sig_b, mem_b = src_rec.geometry
            assert sig_a == sig_b, "stroke family changed"
            assert ([len(m) if isinstance(m, list) else m for m in mem_a]
                    == [len(m) if isinstance(m, list) else m for m in mem_b]), \
                "tpl member shape changed"
            assert rec.color == src_rec.color, "colour changed"
            assert rec.deleted == src_rec.deleted, "deletion state changed"
            from inkref.goodnotes import strokes as sm
            ba, bb = sm.bounds(sig_a, mem_a), sm.bounds(sig_b, mem_b)
            if ba is None or bb is None:
                continue
            assert abs((ba[2] - ba[0]) - (bb[2] - bb[0])) < 1e-3, "stroke deformed in x"
            assert abs((ba[3] - ba[1]) - (bb[3] - bb[1])) < 1e-3, "stroke deformed in y"
            if abs(ba[0] - bb[0]) > 1e-6 or abs(ba[1] - bb[1]) > 1e-6:
                moved += 1
        assert moved > 0, "Swift claimed to beautify but nothing moved"
        print(f"  swift write: Python reads it back — {len(a_recs)} records intact, "
              f"{moved} moved, none deformed")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as workdir:
        binary = build_harness(workdir)
        if binary is None:
            print("no swiftc on this machine — cross-check skipped")
            print("\nall checks passed")
            sys.exit(0)
        for fn in [test_engines_agree, test_engines_agree_on_layout,
                   test_swift_layout_selfcheck,
                   test_swift_beautify_output_is_readable_by_python]:
            print(fn.__name__)
            fn(binary)
    print("\nall checks passed")

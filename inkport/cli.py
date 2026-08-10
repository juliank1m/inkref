"""Command line: analyse, beautify and preview a .goodnotes document.

    python3 -m inkport demo                          # make messy notes, then fix them
    python3 -m inkport analyze notes.goodnotes       # what structure was detected
    python3 -m inkport beautify notes.goodnotes -o clean.goodnotes --preview clean.html
    python3 -m inkport preview notes.goodnotes -o compare.html

Reading and writing GoodNotes is stdlib only; nothing here needs the venv.
"""
import argparse
import os
import sys

from .ai import get_analyzer
from .goodnotes import beautify as bt
from .ink import layout

DEFAULT_TEMPLATE = "samples/test.goodnotes"


def _out(path, suffix, ext):
    stem = os.path.splitext(path)[0]
    return f"{stem}{suffix}{ext}"


def _analyzer(args):
    return get_analyzer(args.ai)


def cmd_analyze(args):
    report = bt.analyze_file(args.input, args.strength, _analyzer(args), args.vision)
    print(report.summary())
    for pr in report.pages:
        if not pr.analysis:
            continue
        print(f"\n  page {pr.page_id[:8]}: x-height ~{pr.analysis.ref_h:.1f}pt, "
              f"pitch {pr.analysis.pitch:.1f}pt, "
              f"indent levels at {[round(x, 1) for x in pr.analysis.levels]}pt")
        for n, line in enumerate(pr.analysis.lines):
            words = "  ".join(f"[{len(w.indices)}]" for w in line.words)
            print(f"    line {n:2d}  y={line.baseline:7.1f}  x={line.box[0]:6.1f}  "
                  f"lvl {line.level}  {len(line.words)} words  {words}")
    return 0


def cmd_beautify(args):
    out = args.output or _out(args.input, ".beautified", ".goodnotes")
    report = bt.beautify_file(args.input, out, args.strength, _analyzer(args), args.vision)
    print(report.summary())
    print(f"\nwrote {out}")
    if args.preview:
        from .preview import preview_html
        preview_html(args.input, args.preview, args.strength,
                     analyzer=_analyzer(args), vision=args.vision)
        print(f"wrote {args.preview}")
    return 0


def cmd_preview(args):
    from .preview import preview_html
    out = args.output or _out(args.input, ".preview", ".html")
    preview_html(args.input, out, args.strength,
                 analyzer=_analyzer(args), vision=args.vision)
    print(f"wrote {out}")
    return 0


def cmd_demo(args):
    from .goodnotes.writer import GoodNotesWriter
    from .ink import handwriting
    from .preview import preview_html

    os.makedirs(args.outdir, exist_ok=True)
    messy = os.path.join(args.outdir, "demo_messy.goodnotes")
    clean = os.path.join(args.outdir, "demo_beautified.goodnotes")
    page = os.path.join(args.outdir, "demo.html")

    doc = handwriting.messy_notes(seed=args.seed)
    _, n = GoodNotesWriter(args.template).write(doc, messy, clear_existing=True)
    print(f"wrote {messy}  ({n} synthetic strokes, deliberately badly laid out)")

    analyzer = _analyzer(args)
    report = bt.beautify_file(messy, clean, args.strength, analyzer, args.vision)
    print(report.summary())
    print(f"wrote {clean}")

    preview_html(messy, page, args.strength, title="demo — messy lecture notes",
                 analyzer=analyzer, vision=args.vision)
    print(f"wrote {page}")
    print("\nimport both .goodnotes files into GoodNotes to check the ink is still native")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="inkport", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def strength_arg(sp):
        sp.add_argument("-s", "--strength", default="balanced",
                        choices=sorted(layout.STRENGTHS),
                        help="how hard to push the layout (default: balanced)")
        sp.add_argument("--ai", default="auto",
                        choices=["auto", "off", "heuristic", "backboard"],
                        help="semantic classification: auto uses Backboard when "
                             "BACKBOARD_API_KEY is set, geometry heuristics otherwise")
        sp.add_argument("--vision", action="store_true",
                        help="send a rendered page image with the classification request")

    a = sub.add_parser("analyze", help="print the detected structure, change nothing")
    a.add_argument("input")
    strength_arg(a)
    a.set_defaults(func=cmd_analyze)

    b = sub.add_parser("beautify", help="write a refactored copy of a document")
    b.add_argument("input")
    b.add_argument("-o", "--output")
    b.add_argument("--preview", metavar="HTML", help="also write a before/after page")
    strength_arg(b)
    b.set_defaults(func=cmd_beautify)

    v = sub.add_parser("preview", help="before/after HTML, writes no document")
    v.add_argument("input")
    v.add_argument("-o", "--output")
    strength_arg(v)
    v.set_defaults(func=cmd_preview)

    d = sub.add_parser("demo", help="generate messy notes, beautify them, preview both")
    d.add_argument("-o", "--outdir", default="generated")
    d.add_argument("--template", default=DEFAULT_TEMPLATE,
                   help="archive to borrow container fields from")
    d.add_argument("--seed", type=int, default=7)
    strength_arg(d)
    d.set_defaults(func=cmd_demo)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

"""Answer the Phase-2 questions about an unknown handwriting PDF.

    python3 -m inkport.pdf.probe file.pdf          (needs venv: pymupdf, pikepdf)

Reports, per page: whether ink lives in annotations or the content stream, whether paths are
stroked centerlines or filled outlines, and what colour/width/opacity is recoverable.

This is a diagnostic. It makes no conversion decisions and imports nothing from ink/ or
goodnotes/ — run it, read it, then write an extractor for what it actually found.
"""
import collections
import sys

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.exit("needs PyMuPDF:  ./venv/bin/pip install pymupdf")


def _fmt_color(c):
    if c is None:
        return "-"
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in c[:3])


def probe(path, max_pages=3, show=6):
    doc = fitz.open(path)
    print(f"file      : {path}")
    print(f"pages     : {doc.page_count}")
    meta = doc.metadata or {}
    print(f"producer  : {meta.get('producer')!r}   creator: {meta.get('creator')!r}")

    for pno in range(min(doc.page_count, max_pages)):
        page = doc[pno]
        print(f"\n=== page {pno}  {page.rect.width:.1f} x {page.rect.height:.1f} pt "
              f"rotation={page.rotation}")

        # --- annotations: the GoodNotes "Editable" export path ---
        annots = list(page.annots() or [])
        kinds = collections.Counter(a.type[1] for a in annots)
        print(f"  annotations : {len(annots)}  {dict(kinds) if kinds else ''}")
        inks = [a for a in annots if a.type[1] == "Ink"]
        if inks:
            print(f"  /Ink annots : {len(inks)}  <- one per stroke is the expected model")
            for a in inks[:show]:
                info = a.info
                inklist = a.vertices
                n_sub = len(inklist) if inklist else 0
                n_pts = sum(len(s) for s in inklist) if inklist and isinstance(
                    inklist[0], (list, tuple)) else (len(inklist) if inklist else 0)
                print(f"    rect={tuple(round(v, 1) for v in a.rect)} "
                      f"border_width={a.border.get('width')} "
                      f"stroke={_fmt_color(a.colors.get('stroke'))} "
                      f"fill={_fmt_color(a.colors.get('fill'))} "
                      f"opacity={a.opacity} /InkList subpaths={n_sub} pts={n_pts}")
                if info.get("content"):
                    print(f"      content={info['content']!r}")
            if len(inks) > show:
                print(f"    ... {len(inks) - show} more")

        # --- page content stream paths: the "Flattened" / other-app path ---
        drawings = page.get_drawings()
        by_type = collections.Counter(d["type"] for d in drawings)   # 's' 'f' 'fs'
        print(f"  content paths: {len(drawings)}  by paint {dict(by_type)}  "
              f"(s=stroke f=fill fs=both)")
        if drawings:
            widths = collections.Counter(round(d.get("width") or 0, 2)
                                         for d in drawings if d["type"] in ("s", "fs"))
            scol = collections.Counter(_fmt_color(d.get("color")) for d in drawings)
            fcol = collections.Counter(_fmt_color(d.get("fill")) for d in drawings)
            ops = collections.Counter(it[0] for d in drawings for it in d["items"])
            print(f"    line widths : {dict(widths.most_common(8))}")
            print(f"    stroke cols : {dict(scol.most_common(6))}")
            print(f"    fill cols   : {dict(fcol.most_common(6))}")
            print(f"    path ops    : {dict(ops)}   (l=line c=bezier re=rect qu=quad)")
            alphas = collections.Counter((d.get("stroke_opacity"), d.get("fill_opacity"))
                                         for d in drawings)
            print(f"    opacities   : {dict(alphas.most_common(4))}")
            for d in drawings[:show]:
                pts = sum(1 for it in d["items"] for _ in it[1:])
                print(f"    {d['type']:>2}  w={d.get('width')}  "
                      f"stroke={_fmt_color(d.get('color'))} fill={_fmt_color(d.get('fill'))} "
                      f"items={len(d['items'])} ops={[it[0] for it in d['items'][:6]]} "
                      f"rect={tuple(round(v, 1) for v in d['rect'])}")

        images = page.get_images(full=True)
        print(f"  images      : {len(images)}"
              + ("   <- RASTER: stroke recovery not possible from this PDF" if images
                 and not drawings and not inks else ""))

        # --- verdict ---
        if inks:
            verdict = ("annotation-based, one /Ink per stroke — best case, "
                       "centerline + width + colour")
        elif by_type.get("s", 0) + by_type.get("fs", 0) > by_type.get("f", 0):
            verdict = "stroked centerlines in the content stream — width recoverable"
        elif by_type.get("f", 0):
            verdict = ("FILLED OUTLINES — no centerline and no width; needs skeletonization")
        elif images:
            verdict = "raster only — no vector ink"
        else:
            verdict = "nothing found"
        print(f"  VERDICT     : {verdict}")

    doc.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        probe(p)
        print()

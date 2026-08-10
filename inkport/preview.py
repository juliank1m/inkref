"""Before / after preview: one self-contained HTML file, no dependencies, no build step.

SPEC §8.9 wants a strong visual comparison and §9 step 4 wants the stroke groups animated
into place. Both fall out of the same fact that makes the transform safe: the result is the
original strokes plus a per-stroke `(dx, dy)`. So the page is rendered **once** and the
offsets are handed to CSS. Toggling replays the exact translation that gets written into
the document — not a re-render, and not a second drawing that could disagree with it.

The structure overlay (SPEC §9 step 3) draws the detected lines, words and indent levels,
which is what shows an audience that the tool understands the page rather than filtering it.
"""
import html
import os

from .goodnotes import beautify as bt
from .goodnotes import render
from .goodnotes.document import Document, POINTS_PER_UNIT, UNITS_PER_POINT
from .ink import layout

CSS = """
:root { color-scheme: light dark; --bg:#0f1115; --fg:#e8eaed; --dim:#8b93a1;
        --accent:#7dd3fc; --panel:#171a21; --edge:#252a34; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 ui-sans-serif,
       -apple-system, "Segoe UI", Roboto, sans-serif; }
header { padding:22px 26px 14px; border-bottom:1px solid var(--edge); }
h1 { margin:0 0 4px; font-size:19px; letter-spacing:.2px; font-weight:600; }
.sub { color:var(--dim); font-size:13px; }
.bar { position:sticky; top:0; z-index:5; display:flex; gap:10px; align-items:center;
       flex-wrap:wrap; padding:12px 26px; background:var(--panel);
       border-bottom:1px solid var(--edge); }
button, label.tog { font:inherit; font-size:13px; padding:7px 14px; border-radius:999px;
       border:1px solid var(--edge); background:#1f242e; color:var(--fg); cursor:pointer; }
button.primary { background:var(--accent); color:#06202b; border-color:transparent;
       font-weight:600; }
label.tog input { margin-right:7px; vertical-align:-1px; }
.state { color:var(--dim); font-size:13px; margin-left:auto; font-variant-numeric:tabular-nums; }
main { padding:26px; display:flex; flex-direction:column; gap:26px; }
.page { background:#fff; border-radius:12px; padding:10px; overflow:auto;
        box-shadow:0 10px 40px rgba(0,0,0,.45); }
.page svg { display:block; width:100%; height:auto; }
.ink { transition: transform .85s cubic-bezier(.2,.8,.25,1); }
.on .ink { transform: translate(var(--dx,0px), var(--dy,0px)); }
.structure { opacity:0; transition:opacity .25s; }
.show-structure .structure { opacity:1; }
table { border-collapse:collapse; font-size:13px; margin-top:10px; }
td, th { padding:4px 16px 4px 0; text-align:left; color:var(--dim); font-weight:400; }
th { color:var(--fg); font-weight:600; }
td.n { font-variant-numeric:tabular-nums; color:var(--fg); }
td.good { color:#86efac; }
.caption { color:var(--dim); font-size:13px; margin:0 0 8px; }
"""

JS = """
const root = document.body;
let on = false;
function set(v){ on = v;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('on', on));
  document.getElementById('state').textContent = on ? 'showing: refactored'
                                                    : 'showing: original';
  document.getElementById('flip').textContent = on ? 'Show original' : 'Beautify';
}
document.getElementById('flip').onclick = () => set(!on);
document.getElementById('struct').onchange = e =>
  document.querySelectorAll('.page').forEach(p =>
    p.classList.toggle('show-structure', e.target.checked));
set(false);
"""


ROLE_COLOR = {"heading": "#f97316", "bullet": "#a78bfa", "equation": "#ef4444",
              "diagram": "#ef4444", "paragraph": "#38bdf8"}


def _overlay(a, roles=None):
    """Detected structure, drawn in GoodNotes units."""
    u = UNITS_PER_POINT
    out = ['<g class="structure">']
    for x in a.levels:
        out.append(f'<line x1="{x * u:.1f}" y1="0" x2="{x * u:.1f}" y2="100000" '
                   f'stroke="#f472b6" stroke-width="1.2" stroke-dasharray="6 6"/>')
    for n, line in enumerate(a.lines):
        role = roles[n] if roles else layout.PARAGRAPH
        tint = ROLE_COLOR.get(role, "#38bdf8")
        x0, y0, x1, y1 = (v * u for v in line.box)
        if role != layout.PARAGRAPH:
            out.append(f'<text x="{x1 + 6:.1f}" y="{line.baseline * u:.1f}" '
                       f'font-size="{14 * u:.1f}" fill="{tint}" '
                       f'font-family="ui-sans-serif,sans-serif">{role}</text>')
        out.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" '
                   f'height="{y1 - y0:.1f}" fill="{tint}" fill-opacity="0.07" '
                   f'stroke="{tint}" stroke-width="1"/>')
        out.append(f'<line x1="{x0:.1f}" y1="{line.baseline * u:.1f}" x2="{x1:.1f}" '
                   f'y2="{line.baseline * u:.1f}" stroke="#f59e0b" stroke-width="1.6"/>')
        for w in line.words:
            wx0, wy0, wx1, wy1 = (v * u for v in w.box)
            out.append(f'<rect x="{wx0:.1f}" y="{wy0:.1f}" width="{wx1 - wx0:.1f}" '
                       f'height="{wy1 - wy0:.1f}" fill="none" stroke="#22c55e" '
                       f'stroke-width="0.9" stroke-dasharray="3 3"/>')
    out.append("</g>")
    return "\n".join(out)


def _metrics_table(before, after):
    rows = [("baseline wobble", "baseline_spread"), ("line pitch", "pitch_spread"),
            ("left margin", "margin_spread"), ("word gaps", "gap_spread")]
    out = ["<table><tr><th>irregularity</th><th>before</th><th>after</th>"
           "<th>change</th></tr>"]
    for label, key in rows:
        b, a = before.get(key, 0.0), after.get(key, 0.0)
        pct = 0.0 if b == 0 else (b - a) / b
        cls = "n good" if pct > 0.01 else "n"
        out.append(f"<tr><td>{label}</td><td class='n'>{b:.2f} pt</td>"
                   f"<td class='n'>{a:.2f} pt</td><td class='{cls}'>{pct:+.0%}</td></tr>")
    out.append("</table>")
    return "".join(out)


def page_html(page, s, analyzer=None, vision=False):
    """One page: a single SVG whose strokes carry their own offset."""
    drawn = bt.page_strokes(page)
    if len(drawn) < 2:
        return None
    boxes = [b for _, b, _, _ in drawn]
    a = layout.analyze(boxes)
    sem = bt.classify(page, a, analyzer, vision)
    roles = sem.roles if sem else None
    offsets = layout.plan(a, s, roles)
    before = layout.metrics(boxes, a, roles)
    after = layout.metrics(layout.moved(boxes, offsets), roles=roles)

    paths = []
    for (rec, _, d, w), (dx, dy) in zip(drawn, offsets):
        r, g, b, _ = rec.color or (0, 0, 0, 1)
        col = f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"
        paths.append(
            f'<path class="ink" d="{d}" fill="none" stroke="{col}" '
            f'stroke-width="{max(w, 0.6):.2f}" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'style="--dx:{dx * UNITS_PER_POINT:.2f}px;--dy:{dy * UNITS_PER_POINT:.2f}px"/>')

    box = render.bbox([("ink", [(d, w, None, 1) for _, _, d, w in drawn])], pad=40.0)
    x0, y0, x1, y1 = box
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'viewBox="{x0:.1f} {y0:.1f} {x1 - x0:.1f} {y1 - y0:.1f}" '
           f'width="{(x1 - x0) * POINTS_PER_UNIT:.0f}" '
           f'height="{(y1 - y0) * POINTS_PER_UNIT:.0f}">'
           f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" '
           f'fill="#ffffff"/>' + "\n".join(paths) + _overlay(a, roles) + "</svg>")

    caption = (f"{len(drawn)} strokes &middot; {len(a.lines)} lines &middot; "
               f"{len(a.words)} words &middot; {len(a.levels)} indent level"
               f"{'s' if len(a.levels) != 1 else ''}")
    if sem:
        named = ", ".join(sorted({r for r in sem.roles if r != layout.PARAGRAPH}))
        caption += (f" &middot; semantics via <b>{html.escape(sem.source)}</b>"
                    + (f": {html.escape(named)}" if named else ""))
    return (f'<section><p class="caption">{caption}</p>'
            f'<div class="page">{svg}</div>{_metrics_table(before, after)}</section>')


def preview_html(in_path, out_path, strength="balanced", title=None,
                 analyzer=None, vision=False):
    """Write a standalone before/after page for `in_path`. Nothing is modified."""
    s = layout.strength(strength)
    doc = Document.open(in_path)
    sections = [h for h in (page_html(p, s, analyzer, vision) for p in doc.pages) if h]
    if not sections:
        raise ValueError(f"{in_path}: no page has enough ink to analyse")
    name = html.escape(title or os.path.basename(in_path))
    body = f"""<meta charset="utf-8"><meta name="viewport"
 content="width=device-width, initial-scale=1"><title>InkRefactor — {name}</title>
<style>{CSS}</style>
<header><h1>InkRefactor</h1>
<div class="sub">{name} &middot; strength <b>{s.name}</b> &middot; every stroke is the
original ink, translated — nothing was redrawn</div></header>
<div class="bar">
  <button id="flip" class="primary">Beautify</button>
  <label class="tog"><input type="checkbox" id="struct">Show detected structure</label>
  <span class="state" id="state"></span>
</div>
<main>{''.join(sections)}</main>
<script>{JS}</script>"""
    with open(out_path, "w") as fh:
        fh.write(body)
    return out_path

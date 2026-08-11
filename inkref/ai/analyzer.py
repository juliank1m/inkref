"""Semantic analysis: what is each detected line?

The rest of InkRef depends on `SemanticAnalyzer` — `analyze(blocks, image) ->
SemanticResult` — and never on Backboard. Two implementations satisfy it:

  `HeuristicAnalyzer`  geometry only, no network, always available. This is the floor.
  `BackboardAnalyzer`  a vision model through Backboard, with the heuristic underneath it.

The second one degrades into the first on every failure worth naming: no API key, network
down, timeout, non-JSON reply, invented block ids, unknown block types, low confidence.
The formatter must keep working when the model does not, so `analyze` never raises.
"""
import json
import os
from dataclasses import dataclass, field

from ..ink import layout
from . import schemas
from .backboard import BackboardClient, BackboardError

SYSTEM = (
    "You classify regions of a handwritten page that has ALREADY been located and read. "
    "You are given each region's id, geometry and recognised text, and sometimes an image "
    "of the page. Reply with JSON only. Never invent an id that was not given to you. "
    "Never transcribe, re-read or re-segment the page, and never return coordinates, "
    "positions, spacing or corrections — another system owns all of those and will discard "
    "them. Your entire job is to name what each given region already is. If you cannot "
    "tell, say 'unknown'; that is a useful answer, not a failure."
)

PROMPT = """Name what each region of this handwritten page is.

The regions below were found by our own recogniser and geometry engine. Their positions
and extents are already settled — do not propose new ones, merge them, or suggest where
anything should go. Coordinates are in points, y down, origin top-left.

{blocks}

Reply with JSON matching exactly this shape and nothing else:
{schema}

Also group regions that are ONE thing and must move together — an equation split across a
numerator, a fraction bar and a denominator; a limit and the expression beneath it; a
diagram and its labels. Grouped regions are translated as a unit and never re-spaced
internally, which is how exponents stay attached to their bases. Group only what you are
sure about; leave ordinary prose ungrouped.

Rules:
- one entry per region id above, using those ids verbatim
- `role` must be one of: {types}
- use `unknown` when you cannot tell; do not guess `paragraph` to fill a gap
- `confidence` is your own 0-1 estimate
- an `equation` or `diagram` is protected from all internal formatting, so use those two
  whenever a region's internal spacing carries meaning
"""


# Classification is billed per token, so one request is kept small on purpose. A page with
# more lines than this is split by column rather than refused (see `_batches`).
MAX_BLOCKS = int(os.environ.get("INKREF_MAX_BLOCKS", "120"))


def _batches(blocks, limit):
    """-> [[block]] small enough to send, or None if no split gets under `limit`.

    Split on the column each line belongs to, which `layout.describe` already knows because
    the layout engine had to separate columns to measure pitch at all. A column is a
    coherent run of reading order, so a classifier sees a sensible document rather than an
    arbitrary slice — and the alternative, cutting every 120 lines, would put a heading in
    one request and the paragraph it introduces in another.
    """
    if len(blocks) <= limit:
        return [blocks]
    by_column = {}
    for b in blocks:
        by_column.setdefault(b.get("column", 0), []).append(b)
    out = [by_column[k] for k in sorted(by_column)]
    return None if any(len(g) > limit for g in out) else out

# Keys the classifier actually reasons from. `bbox` is four floats per line and the model
# is explicitly not asked where anything goes, so shipping full precision geometry is
# paying for tokens that change no answer.
_KEEP = ("id", "text", "words", "strokes", "height_ratio", "indent_level", "gap_above",
         "starts_with_mark", "looks_like_text")

# A recogniser's reading of handwriting is rough, and a long one is mostly tokens. Enough
# to tell a heading from a sentence is all this has to be.
MAX_TEXT = 60


def _compact(blocks):
    """Drop keys the prompt does not use and round what is left."""
    out = []
    for b in blocks:
        row = {}
        for k in _KEEP:
            v = b.get(k)
            if v is None or v == "":
                continue
            if k == "text":
                v = str(v)[:MAX_TEXT]
            row[k] = round(v, 2) if isinstance(v, float) else v
        out.append(row)
    return out


@dataclass
class SemanticResult:
    """What the layout engine consumes. `roles` is one layout role per line, in order."""
    roles: list = field(default_factory=list)
    groups: list = field(default_factory=list)      # [[line index]] that move as one unit
    blocks: list = field(default_factory=list)      # schemas.Block, only what survived
    source: str = "none"                            # backboard | heuristic | none
    warnings: list = field(default_factory=list)

    def label(self, index):
        return self.roles[index] if index < len(self.roles) else layout.PARAGRAPH


class HeuristicAnalyzer:
    """Geometry-only classification. Deliberately timid — it only claims what the shape
    of the page makes obvious, and everything else stays prose."""

    name = "heuristic"

    def analyze(self, blocks, image=None):
        roles, out = [], []
        for i, b in enumerate(blocks):
            role, conf = layout.PARAGRAPH, 0.0
            if b["starts_with_mark"]:
                role, conf = layout.BULLET, 0.75
            elif b["height_ratio"] >= 1.25 and b["words"] <= 4:
                role, conf = layout.HEADING, 0.6
            elif i == 0 and b["words"] <= 4:
                role, conf = layout.HEADING, 0.55
            roles.append(role)
            if conf:
                out.append(schemas.Block(id=b["id"], type=_type_for(role),
                                         confidence=conf))
        return SemanticResult(roles=roles, blocks=out, source=self.name)


def _type_for(role):
    for t, r in schemas.ROLE_FOR_TYPE.items():
        if r == role:
            return t
    return "other"


class BackboardAnalyzer:
    """Vision classification through Backboard, with the heuristic as the floor.

    Calls cost money, so the request is kept small and is made at most twice, and only
    when a second attempt could plausibly succeed: a malformed reply is worth asking
    again for, a timeout or an unsupported model is not. Oversized pages are refused
    outright rather than sent. Whatever happens, the heuristic result is what comes back
    — a slightly worse layout beats a failed one.
    """

    name = "backboard"

    def __init__(self, client=None, fallback=None, attempts=2):
        self.client = client or BackboardClient()
        self.fallback = fallback or HeuristicAnalyzer()
        self.attempts = attempts

    @property
    def available(self):
        return self.client.available

    def analyze(self, blocks, image=None):
        base = self.fallback.analyze(blocks, image)
        if not blocks:
            return base
        if not self.client.available:
            base.warnings.append("BACKBOARD_API_KEY not set; used geometry heuristics")
            return base

        # A dense multi-column sheet detects ~300 lines, which used to be refused outright
        # for exceeding the per-request budget — so on exactly the pages a classifier would
        # help most, it never ran. Send one request per column instead. The columns are
        # already known (the layout engine separates them to measure pitch), each is a
        # coherent run of reading order, and each comfortably fits the budget.
        batches = _batches(blocks, MAX_BLOCKS)
        if batches is None:
            base.warnings.append(
                f"{len(blocks)} lines exceeds the {MAX_BLOCKS}-line budget even split by "
                f"column; used geometry heuristics (raise INKREF_MAX_BLOCKS to override)")
            return base
        if len(batches) > 1:
            return self._batched(batches, base, image)
        return self._one(blocks, base, image)

    def _batched(self, batches, base, image):
        """One request per column, merged. A column that fails leaves its own lines to the
        heuristic and costs the others nothing."""
        roles = list(base.roles)
        order = {b["id"]: i for i, b in enumerate(sum(batches, []))}
        blocks_out, groups, warnings, sources = [], [], [], set()
        for n, batch in enumerate(batches):
            # The image goes with the first request only. It is the same page every time,
            # and paying for it once per column is paying four times for one picture.
            part = self._one(batch, self.fallback.analyze(batch), image if n == 0 else None)
            sources.add(part.source)
            warnings += [f"column {n + 1}: {w}" for w in part.warnings]
            blocks_out += part.blocks
            for b, role in zip(batch, part.roles):
                if b["id"] in order:
                    roles[order[b["id"]]] = role
            groups += [[order[batch[i]["id"]] for i in g if i < len(batch)]
                       for g in part.groups]
        return SemanticResult(roles=roles, groups=[g for g in groups if len(g) > 1],
                              blocks=blocks_out,
                              source=self.name if self.name in sources else "heuristic",
                              warnings=warnings)

    def _one(self, blocks, base, image=None):
        prompt = PROMPT.format(
            blocks=json.dumps(_compact(blocks), separators=(",", ":")),
            schema=schemas.JSON_SCHEMA_HINT,
            types=", ".join(schemas.BLOCK_TYPES))
        ids = [b["id"] for b in blocks]

        warnings = []
        for attempt in range(self.attempts):
            try:
                reply = self.client.ask(
                    prompt if attempt == 0 else prompt + "\nReturn raw JSON only.",
                    system=SYSTEM, image=image)
                payload = schemas.extract_json(reply)
                found, notes = schemas.parse_blocks(reply, ids)
                raw_groups, group_notes = schemas.parse_groups(payload, ids)
                notes = notes + group_notes
            except BackboardError as e:
                # Do NOT retry a transport or configuration failure. A timeout has most
                # likely already run and been billed server-side, and an unsupported model
                # or a bad key fails identically the second time — retrying just doubles
                # the cost of a request that cannot succeed. Only malformed output, below,
                # is worth asking again for.
                warnings.append(str(e))
                break
            except schemas.InvalidModelOutput as e:
                warnings.append(f"attempt {attempt + 1}: {e}")
                continue
            if not found:
                warnings.append(f"attempt {attempt + 1}: nothing survived validation")
                continue

            # A region the model named gets that name. A region it did not is `unknown`,
            # not prose — the model saw the page, and answering on its behalf would license
            # the full prose treatment on a guess we invented. The exception is a region
            # the geometry heuristic made a positive claim about (a bullet mark, an
            # oversized first line): that claim stands on its own evidence and survives.
            by_id = {b.id: b for b in found}
            claimed = {b.id for b in base.blocks}
            roles, unnamed = [], 0
            for i, b in enumerate(blocks):
                if b["id"] in by_id:
                    roles.append(by_id[b["id"]].role)
                elif b["id"] in claimed:
                    roles.append(base.roles[i])
                else:
                    roles.append(layout.UNKNOWN)
                    unnamed += 1
            if unnamed:
                warnings.append(f"{unnamed} of {len(blocks)} regions unnamed by the model; "
                                "left unclassified")
            order = {b["id"]: i for i, b in enumerate(blocks)}
            groups = [[order[i] for i in g if i in order] for g in raw_groups]
            return SemanticResult(roles=roles, groups=[g for g in groups if len(g) > 1],
                                  blocks=found, source=self.name,
                                  warnings=warnings + notes)

        base.warnings.extend(warnings + ["fell back to geometry heuristics"])
        return base


def get_analyzer(mode="auto", client=None):
    """mode: auto | off | heuristic | backboard.

    `auto` uses Backboard when a key is configured and the heuristic otherwise, which is
    what makes the AI layer genuinely optional rather than optional-in-the-README.
    """
    mode = (mode or "auto").lower()
    if mode == "off":
        return None
    if mode == "heuristic":
        return HeuristicAnalyzer()
    if mode == "backboard":
        return BackboardAnalyzer(client=client)
    if mode != "auto":
        raise ValueError(f"unknown ai mode {mode!r}")
    analyzer = BackboardAnalyzer(client=client)
    return analyzer if analyzer.available else HeuristicAnalyzer()

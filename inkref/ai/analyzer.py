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
    "You classify regions of a handwritten page. You are given the geometry of each "
    "detected line and, when available, an image of the page. Reply with JSON only. "
    "Never invent an id that was not given to you. Never suggest coordinates, layout or "
    "corrections — another system owns those. If unsure, say 'other' with low confidence."
)

PROMPT = """Classify each line of this handwritten page.

Lines detected by our geometry engine (coordinates in points, y down, origin top-left):
{blocks}

Reply with JSON matching exactly this shape and nothing else:
{schema}

Rules:
- one entry per line id above, using those ids verbatim
- `type` must be one of: {types}
- `confidence` is your own 0-1 estimate
- `text` is optional and is metadata only; it is never used to redraw anything
"""


# Classification is billed per token, so the payload is kept small on purpose.
MAX_BLOCKS = int(os.environ.get("INKREF_MAX_BLOCKS", "120"))

# Keys the classifier actually reasons from. `bbox` is four floats per line and the model
# is explicitly not asked where anything goes, so shipping full precision geometry is
# paying for tokens that change no answer.
_KEEP = ("id", "words", "strokes", "height_ratio", "indent_level", "gap_above",
         "starts_with_mark", "looks_like_text")


def _compact(blocks):
    """Drop keys the prompt does not use and round what is left."""
    out = []
    for b in blocks:
        row = {}
        for k in _KEEP:
            v = b.get(k)
            if v is None:
                continue
            row[k] = round(v, 2) if isinstance(v, float) else v
        out.append(row)
    return out


@dataclass
class SemanticResult:
    """What the layout engine consumes. `roles` is one layout role per line, in order."""
    roles: list = field(default_factory=list)
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

        # A page is billed per token, so an oversized page is refused rather than sent.
        # A dense four-column sheet detects ~360 lines; that payload plus an image is a
        # large request that mostly buys labels for lines the layout engine will decline
        # to move anyway. Geometry alone is the better trade there.
        if len(blocks) > MAX_BLOCKS:
            base.warnings.append(
                f"{len(blocks)} lines exceeds the {MAX_BLOCKS}-line classification budget; "
                f"used geometry heuristics (raise INKREF_MAX_BLOCKS to override)")
            return base

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
                found, notes = schemas.parse_blocks(reply, ids)
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

            # Start from the heuristic and let validated labels override it, so a model
            # that only classifies half the page still helps with that half.
            roles = list(base.roles)
            by_id = {b.id: b for b in found}
            for i, b in enumerate(blocks):
                if b["id"] in by_id:
                    roles[i] = by_id[b["id"]].role
            return SemanticResult(roles=roles, blocks=found, source=self.name,
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

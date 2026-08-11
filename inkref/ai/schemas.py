"""The contract between a model and the layout engine.

Nothing downstream ever sees a Backboard response, a provider name or a sentence of
English. A model's job is finished the moment its answer becomes a list of `Block`s, and
anything that does not validate is dropped here rather than reaching the geometry.

That boundary is load-bearing: `plan()` moves real ink, so a hallucinated block id or a
type nobody defined has to die at the edge, not three layers in.
"""
import json
import re
from dataclasses import dataclass

from ..ink import layout

# What a model is allowed to say a region is.
BLOCK_TYPES = ("heading", "paragraph", "bullet_list", "equation", "diagram",
               "annotation", "drawing", "other")

# ...and what that means to the layout engine. Anything unmapped is treated as prose,
# which is the conservative default: prose gets the ordinary alignment everyone gets.
ROLE_FOR_TYPE = {
    "heading": layout.HEADING,
    "paragraph": layout.PARAGRAPH,
    "bullet_list": layout.BULLET,
    "equation": layout.EQUATION,
    "diagram": layout.DIAGRAM,
    "annotation": layout.PARAGRAPH,
    "drawing": layout.DIAGRAM,
    "other": layout.PARAGRAPH,
}

MIN_CONFIDENCE = 0.55       # below this the deterministic default is the better bet

JSON_SCHEMA_HINT = """{
  "blocks": [
    {"id": "<one of the given ids>",
     "type": "heading|paragraph|bullet_list|equation|diagram|annotation|drawing|other",
     "confidence": 0.0-1.0,
     "text": "<approximate transcription, optional, metadata only>"}
  ],
  "groups": [
    {"lines": ["<id>", "<id>"], "type": "equation|diagram", "confidence": 0.0-1.0}
  ]
}"""


def parse_groups(payload, valid_ids, min_confidence=0.55):
    """-> (groups as lists of ids, warnings).

    A group says "these lines are one thing" — an equation spread over a numerator, a bar
    and a denominator, or a diagram and its labels. Downstream it becomes a single rigid
    unit, so the worst a wrong group can do is move correct ink together; it can never
    reshape it. Ids are validated exactly like block ids, and a line may only be claimed
    once.
    """
    raw = payload.get("groups")
    if not isinstance(raw, list):
        return [], []
    valid, seen, out, warnings = set(valid_ids), set(), [], []
    for entry in raw:
        if not isinstance(entry, dict):
            warnings.append("dropped a non-object group")
            continue
        try:
            conf = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        ids = [str(i) for i in entry.get("lines", []) if isinstance(i, (str, int))]
        unknown = [i for i in ids if i not in valid]
        ids = [i for i in ids if i in valid and i not in seen]
        if unknown:
            warnings.append(f"group named unknown line(s) {unknown[:3]}")
        if conf < min_confidence:
            warnings.append(f"group at {conf:.2f} below threshold, ignored")
            continue
        if len(ids) < 2:
            continue
        seen.update(ids)
        out.append(ids)
    return out, warnings


@dataclass(frozen=True)
class Block:
    id: str
    type: str
    confidence: float = 0.0
    text: str = ""

    @property
    def role(self):
        return ROLE_FOR_TYPE.get(self.type, layout.PARAGRAPH)


class InvalidModelOutput(ValueError):
    pass


def extract_json(text):
    """Pull the first JSON object out of a model reply.

    `json_output` is documented as ignored whenever files are attached, and a vision call
    attaches the page image, so a fenced block or a sentence of preamble is the norm
    rather than the exception. Balanced-brace scan, not a regex, so nested objects survive.
    """
    if not text:
        raise InvalidModelOutput("empty response")
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("{")
    if start < 0:
        raise InvalidModelOutput("no JSON object in response")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            esc = (ch == "\\") and not esc
            in_str = not (ch == '"' and not esc)
            continue
        if ch == '"':
            in_str, esc = True, False
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as e:
                    raise InvalidModelOutput(f"unparseable JSON: {e}") from None
    raise InvalidModelOutput("unterminated JSON object")


def parse_blocks(text, valid_ids, min_confidence=MIN_CONFIDENCE):
    """-> (blocks, warnings). Never raises on content — only on unusable structure."""
    payload = extract_json(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise InvalidModelOutput("expected an object with a 'blocks' array")

    valid = set(valid_ids)
    blocks, warnings, seen = [], [], set()
    for raw in payload["blocks"]:
        if not isinstance(raw, dict):
            warnings.append("dropped a non-object entry")
            continue
        bid = str(raw.get("id", ""))
        if bid not in valid:
            warnings.append(f"dropped unknown block id {bid!r}")
            continue
        if bid in seen:
            warnings.append(f"dropped duplicate block id {bid!r}")
            continue
        btype = str(raw.get("type", "other")).strip().lower().replace(" ", "_")
        if btype not in BLOCK_TYPES:
            warnings.append(f"{bid}: unknown type {btype!r}, treated as prose")
            btype = "other"
        try:
            conf = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = min(max(conf, 0.0), 1.0)
        if conf < min_confidence:
            warnings.append(f"{bid}: {btype} at {conf:.2f} below threshold, left as prose")
            continue
        text_meta = raw.get("text")
        seen.add(bid)
        blocks.append(Block(id=bid, type=btype, confidence=conf,
                            text=str(text_meta) if isinstance(text_meta, str) else ""))
    return blocks, warnings

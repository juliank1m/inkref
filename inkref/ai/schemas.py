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

# What a model is allowed to say a region is. Deliberately small: every extra name is a
# branch someone has to define a deterministic rule for, and an undefined role is worse
# than no role at all.
BLOCK_TYPES = ("heading", "paragraph", "bullet_list", "bullet_item", "equation",
               "diagram", "annotation", "unknown")

# ...and what that means to the layout engine.
#
# `unknown` is the floor, not `paragraph`. A model that saw the page and could not name a
# region has told us something, and answering "prose" on its behalf would license the full
# prose treatment on the strength of a guess we made up. `unknown` still gets the ordinary
# within-line cleanup (see layout.UNNAMED_ROLES) — it just gets no semantic rule.
ROLE_FOR_TYPE = {
    "heading": layout.HEADING,
    "paragraph": layout.PARAGRAPH,
    "bullet_list": layout.BULLET,
    # A list and an item in it are the same thing at line granularity, which is the only
    # granularity we classify at. Accept both names rather than argue with the model.
    "bullet_item": layout.BULLET,
    "equation": layout.EQUATION,
    "diagram": layout.DIAGRAM,
    "annotation": layout.ANNOTATION,
    "unknown": layout.UNKNOWN,
    # tolerated synonyms — liberal in what we accept, strict in what we act on
    "drawing": layout.DIAGRAM,
    "sketch": layout.DIAGRAM,
    "formula": layout.EQUATION,
    "title": layout.HEADING,
    "section_heading": layout.HEADING,
    "subheading": layout.HEADING,
    "list_item": layout.BULLET,
    "bullet": layout.BULLET,
    "mathematical_expression": layout.EQUATION,
    "math": layout.EQUATION,
    "figure": layout.DIAGRAM,
    "other": layout.UNKNOWN,
}

# A tool call omits `confidence` more often than not: the schema marks it required, but
# the provider does not enforce the schema — measured against the live API, which happily
# returned `{"id": "L0", "type": "title"}` and invented role names outside the enum. So
# the enum and the required list are guidance to the model, not a guarantee to us, and the
# validation below stays exactly as load-bearing as it was.
#
# A missing confidence is treated as this rather than as zero. Calling a named function
# with a named role is a more deliberate act than mentioning a word in a sentence, and
# scoring it zero silently discarded every answer a tool call ever gave.
ASSUMED_CONFIDENCE = 0.6

MIN_CONFIDENCE = 0.55       # below this the deterministic default is the better bet


def classify_tool(ids):
    """The classification request as a function the model must call, not prose to parse.

    Every defence in `parse_blocks` exists because a model asked for JSON in prose returns
    something *near* JSON: fenced, prefaced, truncated, or shaped how it felt like shaping
    it. A tool call is schema-constrained by the provider before it ever reaches us — the
    role has to be one of the listed strings, the confidence has to be a number, and the
    envelope has to be `{"regions": [...]}`.

    The validation downstream stays exactly as it is. This narrows what has to be defended
    against; it does not make the boundary optional, and an id that was never issued is
    still dropped on arrival.
    """
    return [{
        "type": "function",
        "function": {
            "name": "classify_regions",
            "description": ("Record what each already-located region of the page is. "
                            "Call this exactly once with every region id you were given."),
            "parameters": {
                "type": "object",
                "properties": {
                    "regions": {
                        "type": "array",
                        "description": "One entry per region id, in any order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string",
                                       "description": "A region id from the list given.",
                                       "enum": list(ids)},
                                "role": {"type": "string", "enum": list(BLOCK_TYPES),
                                         "description": "What that region is."},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["id", "role", "confidence"],
                        },
                    },
                },
                "required": ["regions"],
            },
        },
    }]

JSON_SCHEMA_HINT = """{
  "regions": [
    {"id": "<one of the given ids>",
     "role": "heading|paragraph|bullet_list|bullet_item|equation|diagram|annotation|unknown",
     "confidence": 0.0-1.0}
  ],
  "groups": [
    {"lines": ["<id>", "<id>"], "role": "equation|diagram", "confidence": 0.0-1.0}
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
        ids = [str(i) for i in entry.get("lines", entry.get("regions", []))
               if isinstance(i, (str, int))]
        unknown = [i for i in ids if i not in valid]
        # dict.fromkeys, not a set: order is the reading order, and a repeated id would
        # put the same strokes in a merged line twice, so every offset applied to it lands
        # twice — a line translated to double what the gate approved.
        ids = list(dict.fromkeys(i for i in ids if i in valid and i not in seen))
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
        return ROLE_FOR_TYPE.get(self.type, layout.UNKNOWN)


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


def _entries(payload):
    """The classification list, under whichever name the model used.

    `regions`/`role` is the shape we ask for. `blocks`/`type` is accepted too: it costs one
    lookup and turns a whole retry — a second billed call — into a non-event.
    """
    for key in ("regions", "blocks"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return None


def parse_blocks(text, valid_ids, min_confidence=MIN_CONFIDENCE):
    """-> (blocks, warnings). Never raises on content — only on unusable structure.

    Anything that does not validate becomes `unknown` rather than disappearing: a region
    the model named badly and a region it never mentioned are the same situation, and both
    must be visibly unclassified rather than quietly treated as prose.
    """
    payload = extract_json(text)
    entries = _entries(payload) if isinstance(payload, dict) else None
    if entries is None:
        raise InvalidModelOutput("expected an object with a 'regions' array")

    valid = set(valid_ids)
    blocks, warnings, seen = [], [], set()
    for raw in entries:
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
        named = raw.get("role", raw.get("type", "unknown"))
        btype = str(named).strip().lower().replace(" ", "_")
        if btype not in ROLE_FOR_TYPE:
            warnings.append(f"{bid}: unrecognised role {btype!r}, left unclassified")
            btype = "unknown"
        try:
            conf = float(raw["confidence"]) if "confidence" in raw else ASSUMED_CONFIDENCE
        except (TypeError, ValueError):
            conf = 0.0
        conf = min(max(conf, 0.0), 1.0)
        if conf < min_confidence:
            warnings.append(f"{bid}: {btype} at {conf:.2f} below threshold, "
                            "left unclassified")
            btype = "unknown"
        text_meta = raw.get("text")
        seen.add(bid)
        blocks.append(Block(id=bid, type=btype, confidence=conf,
                            text=str(text_meta) if isinstance(text_meta, str) else ""))
    return blocks, warnings

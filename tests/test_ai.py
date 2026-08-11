"""The optional semantic layer, with the network faked out.

Run: python3 tests/test_ai.py     (stdlib only)

Nothing here opens a socket: BackboardClient takes an `opener` for exactly this reason, so
every reply — well-formed, fenced, chatty, hallucinated, or an outright transport failure —
is handed to the parser as canned bytes. The point of the suite is the contract in
inkref/ai/analyzer.py: `analyze` never raises, and everything the model gets wrong
degrades into the geometry heuristic instead of reaching the layout engine.
"""
import io
import json
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from inkref.ai import get_analyzer                                  # noqa: E402
from inkref.ai.analyzer import BackboardAnalyzer, HeuristicAnalyzer  # noqa: E402
from inkref.ai.backboard import BackboardClient, Config             # noqa: E402
from inkref.goodnotes import beautify as bt                         # noqa: E402
from inkref.goodnotes.document import Document                      # noqa: E402
from inkref.goodnotes.writer import GoodNotesWriter                 # noqa: E402
from inkref.ink import handwriting                                  # noqa: E402
from inkref.ink import layout                                       # noqa: E402

KEY = "sk-test-not-a-real-key"
TEMPLATE = os.path.join(ROOT, "samples", "test.goodnotes")
FIXTURE = os.path.join(ROOT, "generated", "_ai_fixture.goodnotes")

# What layout.describe() hands a classifier, trimmed to the keys the heuristic reads.
# The heuristic alone labels these heading / paragraph / bullet / paragraph, which is the
# floor every model reply below is measured against.
BLOCKS = [
    {"id": "L0", "words": 2, "height_ratio": 1.40, "starts_with_mark": False},
    {"id": "L1", "words": 6, "height_ratio": 1.00, "starts_with_mark": False},
    {"id": "L2", "words": 3, "height_ratio": 1.00, "starts_with_mark": True},
    {"id": "L3", "words": 5, "height_ratio": 1.00, "starts_with_mark": False},
]
FLOOR = [layout.HEADING, layout.PARAGRAPH, layout.BULLET, layout.PARAGRAPH]


class FakeOpener:
    """urlopen stand-in. `reply` is bytes to hand back, or an exception to raise."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return io.BytesIO(self.reply)


def envelope(text):
    """What the Backboard endpoint wraps a model's reply in."""
    return json.dumps({"status": "COMPLETED", "message": text}).encode()


def blocks_reply(*entries, wrap=envelope):
    payload = json.dumps({"blocks": [
        {"id": i, "type": t, "confidence": c} for i, t, c in entries]})
    return wrap(payload)


def analyzer_for(reply, key=KEY):
    """-> (analyzer, opener). The opener records every request that was 'sent'."""
    opener = FakeOpener(reply)
    client = BackboardClient(Config(api_key=key), opener=opener)
    return BackboardAnalyzer(client=client), opener


def test_well_formed_reply_sets_every_role():
    # deliberately out of order: labels must be matched by id, never by position
    analyzer, opener = analyzer_for(blocks_reply(
        ("L2", "bullet_list", 0.95), ("L0", "heading", 0.95),
        ("L3", "equation", 0.9), ("L1", "paragraph", 0.9)))
    result = analyzer.analyze(BLOCKS)
    assert result.source == "backboard", result.warnings
    assert result.roles == [layout.HEADING, layout.PARAGRAPH, layout.BULLET,
                            layout.EQUATION], result.roles
    assert not result.warnings, result.warnings
    assert len(opener.requests) == 1, "a clean reply must not be retried"
    print("  reply: four labels applied to the right lines regardless of reply order")


def test_fenced_json_still_parses():
    analyzer, _ = analyzer_for(blocks_reply(
        ("L1", "heading", 0.9),
        wrap=lambda p: envelope(f"```json\n{p}\n```")))
    result = analyzer.analyze(BLOCKS)
    assert result.source == "backboard", result.warnings
    assert result.roles[1] == layout.HEADING, result.roles
    print("  fence: a ```json block is unwrapped and applied")


def test_prose_before_the_json_still_parses():
    analyzer, _ = analyzer_for(blocks_reply(
        ("L1", "heading", 0.9),
        wrap=lambda p: envelope(f"Sure! Here is the classification you asked for.\n\n{p}")))
    result = analyzer.analyze(BLOCKS)
    assert result.source == "backboard", result.warnings
    assert result.roles[1] == layout.HEADING, result.roles
    print("  preamble: a chatty prefix is skipped and the object is still found")


def test_invented_block_id_is_dropped():
    """A hallucinated id must die at the schema boundary, not move ink."""
    analyzer, _ = analyzer_for(blocks_reply(
        ("L1", "heading", 0.9), ("L99", "diagram", 0.99)))
    result = analyzer.analyze(BLOCKS)
    assert result.source == "backboard", result.warnings
    assert [b.id for b in result.blocks] == ["L1"]
    assert any("L99" in w for w in result.warnings), result.warnings
    assert result.roles[1] == layout.HEADING, "the valid half must still apply"
    assert layout.DIAGRAM not in result.roles
    print("  hallucination: unknown id dropped with a warning, the rest still applied")


def test_unknown_block_type_becomes_prose():
    analyzer, _ = analyzer_for(blocks_reply(("L0", "sonnet", 0.95)))
    result = analyzer.analyze(BLOCKS)
    assert result.source == "backboard", result.warnings
    # the heuristic called L0 a heading; an uninterpretable type overrides it to prose
    assert result.roles[0] == layout.PARAGRAPH, result.roles
    assert any("sonnet" in w for w in result.warnings), result.warnings
    print("  vocabulary: an undefined type is treated as prose instead of raising")


def test_low_confidence_is_ignored():
    analyzer, _ = analyzer_for(blocks_reply(
        ("L2", "equation", 0.95), ("L1", "heading", 0.30)))
    result = analyzer.analyze(BLOCKS)
    assert result.roles[2] == layout.EQUATION, result.roles
    assert result.roles[1] == FLOOR[1], "a 0.30 label must not override the default"
    assert any("below threshold" in w for w in result.warnings), result.warnings
    print("  confidence: a 0.30 label is left as prose, a 0.95 one is applied")


def test_transport_failures_fall_back_to_the_heuristic():
    """Every failure worth naming ends in a usable layout, never an exception."""
    cases = {
        "http error": urllib.error.HTTPError(
            "https://app.backboard.io/api/threads/messages", 502, "Bad Gateway", {}, None),
        "timeout": TimeoutError("timed out"),
        "non-JSON body": b"<html>upstream is having a moment</html>",
        "empty body": b"",
    }
    for label, reply in cases.items():
        analyzer, opener = analyzer_for(reply)
        result = analyzer.analyze(BLOCKS)
        assert result.source == "heuristic", (label, result.source)
        assert result.roles == FLOOR, (label, result.roles)
        assert result.warnings[-1] == "fell back to geometry heuristics", \
            (label, result.warnings)
        assert len(opener.requests) == analyzer.attempts, (label, len(opener.requests))
    print(f"  failure: {', '.join(cases)} each degrade to geometry, none raise")


def test_auto_without_a_key_is_the_heuristic():
    assert not Config.from_env({}).configured, "an empty environment must read as off"
    keyless = BackboardClient(Config(api_key=""), opener=FakeOpener(b""))
    assert isinstance(get_analyzer("auto", client=keyless), HeuristicAnalyzer)
    assert isinstance(get_analyzer("auto", client=BackboardClient(
        Config(api_key=KEY), opener=FakeOpener(b""))), BackboardAnalyzer)
    print("  auto: no API key selects the heuristic, a key selects Backboard")


def test_off_disables_the_layer_entirely():
    assert get_analyzer("off") is None
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    GoodNotesWriter(TEMPLATE).write(handwriting.messy_notes(), FIXTURE, clear_existing=True)
    report = bt.beautify_document(Document.open(FIXTURE), analyzer=None)
    assert report.moved > 0, "the deterministic path must beautify without a classifier"
    assert all(p.semantic is None for p in report.pages)
    print(f"  off: analyzer=None, {report.moved}/{report.strokes} strokes still moved")


def test_the_key_travels_in_a_header_not_the_url():
    analyzer, opener = analyzer_for(blocks_reply(("L0", "heading", 0.9)))
    analyzer.analyze(BLOCKS)
    req = opener.requests[0]
    sent = {k.lower(): v for k, v in req.header_items()}
    assert sent.get("x-api-key") == KEY, sent
    assert req.full_url.endswith("/threads/messages"), req.full_url
    assert KEY not in req.full_url, "the API key must never reach a URL or a log line"
    print("  transport: key in X-API-Key only, absent from the request URL")


if __name__ == "__main__":
    for fn in [test_well_formed_reply_sets_every_role,
               test_fenced_json_still_parses,
               test_prose_before_the_json_still_parses,
               test_invented_block_id_is_dropped,
               test_unknown_block_type_becomes_prose,
               test_low_confidence_is_ignored,
               test_transport_failures_fall_back_to_the_heuristic,
               test_auto_without_a_key_is_the_heuristic,
               test_off_disables_the_layer_entirely,
               test_the_key_travels_in_a_header_not_the_url]:
        print(fn.__name__)
        fn()
    if os.path.exists(FIXTURE):
        os.remove(FIXTURE)
    print("\nall checks passed")

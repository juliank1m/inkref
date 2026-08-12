"""Backboard.io transport. The only file in the project that knows this vendor exists.

    POST {base}/threads/messages          header: X-API-Key

Stdlib `urllib` on purpose — the GoodNotes read/write path has no third-party dependency
and the AI layer is optional, so adding an HTTP client for it would make the whole tool
harder to install for a feature that must be allowed to be absent.

Two quirks of the API shape the code:

  * `json_output: true` asks for a JSON object back, but the docs say it is **ignored
    whenever files are attached** — and a vision call attaches the page image. So the
    strict path and the vision path need different parsing; see `schemas.extract_json`.
  * images go through the ordinary `files` multipart field, not a separate vision field.

Configuration is environment only. Nothing here is read from a file in the repo:

    BACKBOARD_API_KEY     required; absent means the AI layer is simply off
    BACKBOARD_BASE_URL    default https://app.backboard.io/api
    BACKBOARD_PROVIDER    default anthropic
    BACKBOARD_MODEL       default claude-haiku-4-5-20251001  (any name from GET /models)
    BACKBOARD_TIMEOUT     seconds, default 30
"""
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://app.backboard.io/api"
DEFAULT_PROVIDER = "anthropic"
# Backboard's own docs still show claude-sonnet-4-20250514, which their API rejects as
# unsupported. Taken from a live GET /models instead. Any name in that catalog works via
# BACKBOARD_MODEL.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT = 30.0


def _dotenv(*paths):
    """`KEY=value` lines from a .env file. The real environment always wins.

    A shell `export` does not survive into a separate process, so the key has to live in a
    file for anything but that one shell to see it. Three places are checked, first match
    wins per key, and a real environment variable still beats all of them:

        ./.env              whatever directory you happen to be in
        <repo>/.env         so the CLI works when run from anywhere
        ~/.inkref.env       machine-wide, outside the repo entirely

    The repo path matters: keying only off the working directory means `python3 -m inkref`
    silently loses the key the moment you run it from somewhere else. Nothing here is ever
    written back or logged, and both .env paths are gitignored.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    paths = paths or (os.path.join(os.getcwd(), ".env"),
                      os.path.join(repo, ".env"),
                      os.path.expanduser("~/.inkref.env"))
    out = {}
    for path in paths:
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    out.setdefault(key.strip(), value.strip().strip("'\""))
        except OSError:
            continue
    return out


class BackboardError(RuntimeError):
    """Anything that stopped a usable answer coming back. Always recoverable: every
    caller falls back to the deterministic path."""


@dataclass(frozen=True)
class Config:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, env=None):
        env = dict(_dotenv(), **os.environ) if env is None else env
        try:
            timeout = float(env.get("BACKBOARD_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        return cls(
            api_key=env.get("BACKBOARD_API_KEY", "").strip(),
            base_url=env.get("BACKBOARD_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            provider=env.get("BACKBOARD_PROVIDER", DEFAULT_PROVIDER),
            model=env.get("BACKBOARD_MODEL", DEFAULT_MODEL),
            timeout=timeout,
        )

    @property
    def configured(self):
        return bool(self.api_key)


def _tool_arguments(payload):
    """-> the arguments string of the first tool call, or None if there was no call.

    Backboard pauses a run at `REQUIRES_ACTION` and hands back `tool_calls`, each with
    `function.arguments` already shaped to the schema that was sent. There is nothing to
    execute here — the "tool" exists only to make the model answer in a fixed form, so the
    run is never resumed and the arguments are the whole answer.
    """
    calls = payload.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    fn = calls[0].get("function") if isinstance(calls[0], dict) else None
    if not isinstance(fn, dict):
        return None
    args = fn.get("arguments")
    return args if isinstance(args, str) and args.strip() else None


def _multipart(fields, image=None, filename="page.png"):
    """-> (content_type, body). Small enough to hand-roll; one page image, a few fields."""
    boundary = f"----inkref{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                f"{value}\r\n").encode()
    if image:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode()
        out += image + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(out)


class BackboardClient:
    def __init__(self, config=None, opener=None):
        self.config = config or Config.from_env()
        # injectable so tests never touch the network
        self._open = opener or urllib.request.urlopen

    @property
    def available(self):
        return self.config.configured

    def ask(self, content, system=None, image=None, tools=None):
        """One stateless turn. -> the model's reply text, or a tool call's arguments.

        No `thread_id` is ever sent and memory stays off: classifying a page must not
        depend on, or leak into, anything the user asked before.

        `tools` asks Backboard to constrain the answer to a named function's schema
        instead of trusting prose to contain valid JSON. When the model calls it, the run
        comes back `REQUIRES_ACTION` with the arguments already shaped, and that string is
        what is returned — the caller parses one object rather than hunting for one.
        """
        if not self.available:
            raise BackboardError("BACKBOARD_API_KEY is not set")

        fields = {
            "content": content,
            "stream": "false",
            "memory": "off",
            "web_search": "off",
            "llm_provider": self.config.provider,
            "model_name": self.config.model,
        }
        if system:
            fields["system_prompt"] = system
        if tools:
            fields["tools"] = tools

        url = f"{self.config.base_url}/threads/messages"
        headers = {"X-API-Key": self.config.api_key, "Accept": "application/json"}
        if image:
            # json_output is documented as ignored once files are attached, so it is not
            # sent here — the reply is parsed leniently instead. Tools go the same way:
            # a multipart field cannot carry a nested schema, so a vision call keeps the
            # prose path and its lenient parser.
            ctype, body = _multipart({k: v for k, v in fields.items() if k != "tools"},
                                     image)
        else:
            ctype = "application/json"
            payload = {**fields, "stream": False}
            # `json_output` and `tools` are mutually exclusive in practice: asking for a
            # JSON response puts the model in a mode where it answers instead of calling,
            # and the run comes back with prose in `content` and no `tool_calls` at all.
            # Measured against the live API — with both set the tool was silently ignored.
            if not tools:
                payload["json_output"] = True
            body = json.dumps(payload).encode()
        headers["Content-Type"] = ctype

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._open(req, timeout=self.config.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace") if e.fp else ""
            raise BackboardError(f"HTTP {e.code} from Backboard: {detail}") from None
        except urllib.error.URLError as e:
            raise BackboardError(f"cannot reach Backboard: {e.reason}") from None
        except (TimeoutError, OSError) as e:
            raise BackboardError(f"Backboard request failed: {e}") from None
        except json.JSONDecodeError:
            raise BackboardError("Backboard returned a non-JSON body") from None

        if not isinstance(payload, dict):
            raise BackboardError("Backboard returned an unexpected body")
        if str(payload.get("status", "")).upper() == "FAILED":
            # The reason lives in `content` — "Model 'x' is not supported", a quota
            # message, and so on. Reporting a bare FAILED turns a one-line configuration
            # fix into a debugging session, and the fallback hides it from the user.
            reason = str(payload.get("content") or "").strip()
            raise BackboardError(
                f"Backboard run FAILED: {reason[:200]}" if reason
                else "Backboard reported the run as FAILED")
        # `content` first, NOT `message`. Measured against the live API: on a successful
        # run `message` is the envelope status "Message added successfully" and `content`
        # carries the model's reply. Reading `message` first parses that status string as
        # the answer, JSON extraction fails, and every call silently falls back to the
        # heuristics — the AI layer looks wired up and does nothing.
        # A tool call is the answer when one was asked for. `content` is null in that
        # case, so reading it first would look like an empty reply.
        call = _tool_arguments(payload)
        if call is not None:
            return call
        text = payload.get("content") or payload.get("message")
        if not text:
            raise BackboardError("Backboard returned no message text")
        return text

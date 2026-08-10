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
    BACKBOARD_MODEL       default claude-sonnet-4-20250514
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
DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_TIMEOUT = 30.0


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
        env = os.environ if env is None else env
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


def _multipart(fields, image=None, filename="page.png"):
    """-> (content_type, body). Small enough to hand-roll; one page image, a few fields."""
    boundary = f"----inkport{uuid.uuid4().hex}"
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

    def ask(self, content, system=None, image=None):
        """One stateless turn. -> the model's reply text.

        No `thread_id` is ever sent and memory stays off: classifying a page must not
        depend on, or leak into, anything the user asked before.
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

        url = f"{self.config.base_url}/threads/messages"
        headers = {"X-API-Key": self.config.api_key, "Accept": "application/json"}
        if image:
            # json_output is documented as ignored once files are attached, so it is not
            # sent here — the reply is parsed leniently instead.
            ctype, body = _multipart(fields, image)
        else:
            ctype = "application/json"
            body = json.dumps({**fields, "stream": False, "json_output": True}).encode()
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
            raise BackboardError("Backboard reported the run as FAILED")
        text = payload.get("message") or payload.get("content")
        if not text:
            raise BackboardError("Backboard returned no message text")
        return text

"""Model backends for claim extraction.

Extraction is the one place this project needs a language model, and the job is
narrow: read prose, emit structured claims. Any capable model can do it, so the
backend is pluggable rather than assumed.

Two are supported:

* **Anthropic** — the default. Best extraction quality, which matters because
  this step sets ``P(plays)`` and that dominates every forecast. Paid, but small:
  roughly $0.14–$0.71 per run depending on the model.
* **Google Gemini** — has a real free tier through AI Studio, with rate limits
  that comfortably fit a research run of ~60 documents. Free is free.

Both return the same ``ExtractionResult``, so nothing downstream knows or cares
which one ran. The tier rules, the resolver and the optimiser are unchanged.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_DEFAULT = "claude-opus-5"
GEMINI_DEFAULT = "gemini-2.5-flash"

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

# The shape both providers must return. Kept as a plain JSON Schema because
# that is the lowest common denominator — Anthropic takes a Pydantic model,
# Gemini takes a schema dict, and this converts cleanly to either.
CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string"},
                    "club": {"type": "string"},
                    "claim": {"type": "string"},
                    "impact": {
                        "type": "string",
                        "enum": [
                            "availability",
                            "minutes",
                            "role",
                            "set_pieces",
                            "form",
                            "fixture",
                        ],
                    },
                    "hedged": {"type": "boolean"},
                    "attributed_to": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "player_name",
                    "club",
                    "claim",
                    "impact",
                    "hedged",
                    "attributed_to",
                    "confidence",
                ],
            },
        }
    },
    "required": ["claims"],
}


class ExtractionBackend(ABC):
    """Turns one document into raw claim dictionaries."""

    name: str
    model: str

    @abstractmethod
    def extract(self, system: str, document: str) -> list[dict[str, Any]]:
        """Return claim dicts. Raises on failure; the caller decides what that costs."""


class AnthropicBackend(ExtractionBackend):
    """Extraction via the Anthropic SDK, using structured outputs."""

    name = "anthropic"

    def __init__(self, client: Any, model: str = ANTHROPIC_DEFAULT) -> None:
        self.client = client
        self.model = model

    def extract(self, system: str, document: str) -> list[dict[str, Any]]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": document}],
            output_config={"format": {"type": "json_schema", "schema": CLAIM_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "{}")
        return json.loads(text).get("claims", [])


class GeminiBackend(ExtractionBackend):
    """Extraction via the Gemini REST API.

    Uses REST rather than the Google SDK so the free option costs no extra
    dependency — ``httpx`` is already here for everything else.

    Gemini's ``responseSchema`` gives the same guarantee Anthropic's structured
    outputs do: the response parses, or the request fails loudly.
    """

    name = "gemini"

    def __init__(
        self, api_key: str, model: str = GEMINI_DEFAULT, timeout: float = 60.0
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def extract(self, system: str, document: str) -> list[dict[str, Any]]:
        response = httpx.post(
            f"{GEMINI_ENDPOINT}/{self.model}:generateContent",
            params={"key": self.api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": document}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": CLAIM_SCHEMA,
                    "temperature": 0.0,
                },
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(f"gemini returned {response.status_code}: {response.text[:200]}")

        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            # A safety block or an empty response. Not an error worth failing the
            # run over — one unread document costs one document.
            log.debug("gemini returned no candidates: %s", str(payload)[:200])
            return []
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        return json.loads(text or "{}").get("claims", [])


def build_backend(
    *,
    provider: str,
    model: str | None,
    anthropic_key: str | None,
    gemini_key: str | None,
) -> ExtractionBackend | None:
    """Pick a backend from configuration and available keys.

    ``provider: auto`` prefers Anthropic for quality and falls back to Gemini,
    so the pipeline does the best it can with whatever is configured rather than
    failing or silently doing less.

    Returns None when no key is available at all — extraction is then skipped and
    the run continues on Tier 1 data, which is a degradation rather than a fault.
    """
    choice = (provider or "auto").lower()

    if choice in ("auto", "anthropic"):
        client = _anthropic_client(anthropic_key)
        if client is not None:
            return AnthropicBackend(client, model or ANTHROPIC_DEFAULT)
        if choice == "anthropic":
            log.warning("provider is 'anthropic' but no key or SDK is available")
            return None

    if choice in ("auto", "gemini") and gemini_key:
        return GeminiBackend(gemini_key, model or GEMINI_DEFAULT)

    return None


def _anthropic_client(api_key: str | None) -> Any | None:
    """Construct an Anthropic client, or None if unavailable.

    An unset key does not always mean no credentials — the SDK also resolves an
    ``ant auth login`` profile — so a bare constructor is attempted regardless.
    """
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    except Exception as exc:  # missing credentials is a normal state, not a fault
        log.debug("no Anthropic credentials: %s", exc)
        return None

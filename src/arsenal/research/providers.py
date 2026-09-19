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
import random
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_DEFAULT = "claude-opus-5"
# Google retires models and refuses them to new keys: gemini-2.5-flash now 404s
# with "no longer available to new users". Pinned to a specific version rather
# than a rolling `-latest` alias, so a silent upstream change cannot alter
# extraction behaviour mid-season.
#
# `arsenal sources` lists what a given key can actually reach — availability
# varies by key and by load, and the newest model is not always the reachable
# one.
GEMINI_DEFAULT = "gemini-3.5-flash"

# Free-tier quotas, both measured from live 429 responses:
#   GenerateRequestsPerMinutePerProjectPerModel-FreeTier = 5
#   GenerateRequestsPerDayPerProjectPerModel-FreeTier    = 20
#
# The daily cap is the binding one, and it is why extraction batches several
# documents into each request rather than sending one apiece. A research run of
# ~55 articles is 55 requests one-at-a-time — nearly three days of quota — but
# around seven when batched, which fits comfortably.
GEMINI_FREE_TIER_RPM = 5
GEMINI_FREE_TIER_RPD = 20

# Free-tier models are frequently either overloaded (503 "high demand") or out of
# daily quota (429), and *which* one is unavailable changes through the day. Both
# quotas are per model, so moving to another one is a genuine reset rather than a
# workaround.
#
# Ordered by preference: a full Flash model first, lite variants after. Lite is
# weaker at preserving hedging, which matters here — "should be fit" and "is fit"
# carry different weight downstream — so it is a fallback, not a default.
GEMINI_FALLBACKS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

# The free tier returns 503 "high demand" and 429 under load often enough that
# not retrying would lose a meaningful share of every research run.
RETRY_STATUSES = (429, 503, 500, 502, 504)

# Retry hard only on the last model available. When another model can be tried
# instead, moving on immediately is both faster and more likely to work: an
# overloaded model stays overloaded for minutes, whereas a different one is a
# fresh quota and a different queue. Backing off four times before switching
# wastes minutes to learn nothing.
MAX_ATTEMPTS_LAST_RESORT = 4
MAX_ATTEMPTS_WITH_FALLBACK = 1

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
                    "document": {
                        "type": "integer",
                        "description": "1-based number of the document this claim came from",
                    },
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
                    "document",
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
        self,
        api_key: str,
        model: str = GEMINI_DEFAULT,
        timeout: float = 60.0,
        requests_per_minute: int = GEMINI_FREE_TIER_RPM,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.min_interval = 60.0 / max(requests_per_minute, 1)
        self._last_request_at = 0.0
        # Preferred model first, then the rest as fallbacks, without duplicates.
        self._candidates = [model] + [m for m in GEMINI_FALLBACKS if m != model]

    def _throttle(self) -> None:
        """Pace requests to stay inside the quota.

        Cheaper than discovering the limit through 429s: a refused request still
        costs a round trip, and the retry that follows arrives during the same
        exhausted window.
        """
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def extract(self, system: str, document: str) -> list[dict[str, Any]]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": document}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": CLAIM_SCHEMA,
                "temperature": 0.0,
            },
        }

        # Try each model in turn. Quota and overload are both per model, so a
        # refusal from one says nothing about the next — and losing an article
        # because the popular model was busy would be a poor trade.
        errors: list[str] = []
        for position, candidate in enumerate(self._candidates):
            is_last = position == len(self._candidates) - 1
            attempts = MAX_ATTEMPTS_LAST_RESORT if is_last else MAX_ATTEMPTS_WITH_FALLBACK
            try:
                payload = self._post_with_retry(body, model=candidate, attempts=attempts)
            except RuntimeError as exc:
                errors.append(f"{candidate}: {str(exc)[:80]}")
                continue
            if candidate != self.model:
                log.info("extracted with %s after %s was unavailable", candidate, self.model)
                # Stick with what works for the rest of the run rather than
                # rediscovering the same outage on every batch.
                self.model = candidate
            return _claims_from(payload)

        raise RuntimeError("every Gemini model was unavailable:\n  " + "\n  ".join(errors))

    def _post_with_retry(
        self,
        body: dict[str, Any],
        *,
        model: str | None = None,
        attempts: int = MAX_ATTEMPTS_LAST_RESORT,
    ) -> dict[str, Any]:
        """POST with backoff on the transient failures the free tier produces.

        Overload is routine here rather than exceptional, so a single attempt
        would silently drop articles from every run. A model that is *retired*
        gets a clear message instead of a retry loop - no amount of waiting
        fixes a 404.
        """
        last: str = ""
        for attempt in range(attempts):
            self._throttle()
            response = httpx.post(
                f"{GEMINI_ENDPOINT}/{model or self.model}:generateContent",
                params={"key": self.api_key},
                json=body,
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json()

            last = response.text[:200]

            if response.status_code == 404 and "no longer available" in response.text:
                raise RuntimeError(
                    f"the model '{model or self.model}' has been retired by Google.\n"
                    f"{last}\n"
                    "Set research.model in config.yaml to a current one; "
                    "`arsenal sources` lists what your key can reach."
                )
            if response.status_code not in RETRY_STATUSES:
                raise RuntimeError(f"gemini returned {response.status_code}: {last}")

            if attempt < attempts - 1:
                # Honour the server's own retryDelay when it gives one — it
                # knows when the quota window reopens and guessing shorter just
                # burns another refusal. Otherwise back off exponentially, with
                # jitter so ~60 queued requests do not retry in lockstep.
                delay = _retry_delay(response) or (2**attempt) + random.uniform(0, 1)
                log.info(
                    "gemini %s on attempt %d; retrying in %.1fs",
                    response.status_code,
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"unavailable after {attempts} attempt(s): {last}")


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


def _retry_delay(response: httpx.Response) -> float | None:
    """The server's advised wait, when it supplies one.

    Gemini returns `retryDelay: "45s"` in the error details on a quota refusal.
    """
    try:
        for detail in response.json().get("error", {}).get("details", []):
            raw = detail.get("retryDelay")
            if isinstance(raw, str) and raw.endswith("s"):
                return float(raw[:-1])
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def _claims_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the claim list out of a Gemini response.

    An empty candidate list means a safety block or a truncated generation —
    disappointing, but one unread batch is not worth failing a run over.
    """
    candidates = payload.get("candidates") or []
    if not candidates:
        log.debug("gemini returned no candidates: %s", str(payload)[:200])
        return []
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    return json.loads(text or "{}").get("claims", [])

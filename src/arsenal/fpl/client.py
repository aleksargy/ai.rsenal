"""Read client for the FPL API.

Caches to disk, rate-limits, retries with backoff, and persists raw bytes before
parsing. That last part matters: when a payload shape changes mid-season, the
bytes that broke it are irreproducible after the fact.

FPL is a free service run for players, not an API product. A full pipeline run
should make a handful of requests, not hundreds.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .schemas import Bootstrap, Fixture, MyTeam

log = logging.getLogger(__name__)

BASE_URL = "https://fantasy.premierleague.com/api"

# Identify honestly. An anonymous scraper is indistinguishable from an abusive one.
USER_AGENT = "ai.rsenal/0.1 (personal FPL assistant; +https://github.com/)"

# bootstrap-static changes meaningfully once a day (prices at ~01:30 UTC), plus
# during live matches. Six hours is a safe default well inside that.
DEFAULT_TTL_SECONDS = 6 * 60 * 60

# Never poll faster than this, even during a live gameweek.
MIN_REQUEST_INTERVAL = 1.0


class FPLError(RuntimeError):
    """A request failed in a way the caller must handle."""


class AuthRequired(FPLError):
    """Endpoint returned 403.

    Note this is returned both for *no* session and for an *expired* one, so a
    403 alone does not say which. Probe ``my-team/`` to distinguish.
    """


class FPLClient:
    """Synchronous read client with an on-disk cache.

    Pass ``session_cookies`` to reach authenticated endpoints. See the ``fpl-api``
    skill for why those cookies must come from a real browser.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        session_cookies: dict[str, str] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        raw_dir: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = raw_dir
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._last_request_at = 0.0
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            cookies=session_cookies or {},
            timeout=timeout,
            follow_redirects=True,
        )

    def __enter__(self) -> FPLClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- internals

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key.replace('/', '_').strip('_')}.json"

    def _read_cache(self, key: str, ttl: int | None) -> Any | None:
        ttl = self.ttl_seconds if ttl is None else ttl
        if ttl <= 0:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > ttl:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry is a nuisance, never a reason to fail a run.
            log.warning("discarding unreadable cache entry %s", path)
            return None

    def _write_cache(self, key: str, payload: Any) -> None:
        self._cache_path(key).write_text(json.dumps(payload), encoding="utf-8")

    def _persist_raw(self, key: str, payload: Any) -> None:
        if self.raw_dir is None:
            return
        name = f"{key.replace('/', '_').strip('_')}.json"
        (self.raw_dir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.monotonic()

    def get(
        self,
        path: str,
        *,
        cache_key: str | None = None,
        ttl: int | None = None,
        retries: int = 3,
    ) -> Any:
        """GET a path, honouring the cache. Returns parsed JSON."""
        key = cache_key or path
        cached = self._read_cache(key, ttl)
        if cached is not None:
            log.debug("cache hit %s", key)
            return cached

        last_error: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            try:
                response = self._client.get(path)
            except httpx.RequestError as exc:
                last_error = exc
                log.warning("request error on %s (attempt %d): %s", path, attempt + 1, exc)
                time.sleep(2**attempt)
                continue

            if response.status_code == 403:
                raise AuthRequired(
                    f"{path} returned 403 — session missing or expired. "
                    "Re-seed with `arsenal auth login`."
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = FPLError(f"{path} returned {response.status_code}")
                log.warning("retryable %s on %s", response.status_code, path)
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise FPLError(f"{path} returned {response.status_code}: {response.text[:200]}")

            payload = response.json()
            self._write_cache(key, payload)
            self._persist_raw(key, payload)
            return payload

        raise FPLError(f"{path} failed after {retries} attempts") from last_error

    # ------------------------------------------------------------- public reads

    def bootstrap(self, *, ttl: int | None = None) -> Bootstrap:
        """The main payload: players, teams, gameweeks, scoring and rules config."""
        return Bootstrap.model_validate(
            self.get("/bootstrap-static/", cache_key="bootstrap", ttl=ttl)
        )

    def fixtures(self, event: int | None = None, *, ttl: int | None = None) -> list[Fixture]:
        path = "/fixtures/" if event is None else f"/fixtures/?event={event}"
        key = "fixtures" if event is None else f"fixtures_{event}"
        return [Fixture.model_validate(f) for f in self.get(path, cache_key=key, ttl=ttl)]

    def element_summary(self, element_id: int, *, ttl: int | None = None) -> dict[str, Any]:
        """Per-player match history, past seasons, and upcoming fixtures."""
        return self.get(
            f"/element-summary/{element_id}/", cache_key=f"element_{element_id}", ttl=ttl
        )

    def event_live(self, event: int, *, ttl: int | None = None) -> dict[str, Any]:
        """Live per-player stats for a gameweek. Large; cache briefly when in-play."""
        return self.get(f"/event/{event}/live/", cache_key=f"live_{event}", ttl=ttl)

    def event_status(self) -> dict[str, Any]:
        """Whether bonus and league tables have been processed. Never cached."""
        return self.get("/event-status/", ttl=0)

    def entry(self, team_id: int, *, ttl: int | None = None) -> dict[str, Any]:
        return self.get(f"/entry/{team_id}/", cache_key=f"entry_{team_id}", ttl=ttl)

    def entry_history(self, team_id: int, *, ttl: int | None = None) -> dict[str, Any]:
        """Season history and — critically — the authoritative record of chips used."""
        return self.get(
            f"/entry/{team_id}/history/", cache_key=f"entry_{team_id}_history", ttl=ttl
        )

    def set_piece_notes(self, *, ttl: int | None = None) -> dict[str, Any]:
        """Editorially maintained per-club set-piece taker notes. High signal."""
        return self.get("/team/set-piece-notes/", cache_key="set_piece_notes", ttl=ttl)

    # -------------------------------------------------------- authenticated

    def my_team(self, team_id: int) -> MyTeam:
        """Current squad with purchase prices. Requires a valid session.

        Never cached: this is the server's truth about budget and free transfers,
        and it must be re-read immediately before any write. A price that moved
        between read and write causes the server to reject the whole payload.
        """
        return MyTeam.model_validate(self.get(f"/my-team/{team_id}/", ttl=0))

    def is_authenticated(self, team_id: int) -> bool:
        """Probe whether the current session can actually read privileged data."""
        try:
            self.my_team(team_id)
        except AuthRequired:
            return False
        except FPLError:
            # A network blip is not proof of expiry; report unknown as False but
            # let the caller see the log rather than silently re-seeding.
            log.warning("auth probe failed for a non-auth reason", exc_info=True)
            return False
        return True

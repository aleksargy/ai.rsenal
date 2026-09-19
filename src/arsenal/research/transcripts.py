"""Transcript fetching that does not get itself blocked.

YouTube rate-limits transcript scraping hard and blocks the offending IP for a
while — which is exactly what happened here, fetching a dozen in quick
succession. The error also notes that cloud-provider ranges are blocked by
default, so hosted CI will never see a transcript at all.

The block is at the **IP level on YouTube's `timedtext` endpoint**, not in any
particular client. Verified the hard way: `youtube-transcript-api`, `yt-dlp`, and
a `fetch()` issued from inside a real logged-in browser page all receive the same
429. So no client-side trick avoids it — only a different IP, or waiting.

Three things make this sustainable from a home connection:

* **Cache permanently.** A transcript for a published video never changes, so
  every video is fetched at most once, ever. Across a season that is the
  difference between thousands of requests and a few hundred.
* **Pace requests.** Several seconds apart, not as fast as the loop can run.
* **Stop at the first block.** Once YouTube refuses, every later request in that
  run is refused too; continuing only deepens the block.

The alternative — descriptions alone — is worth very little. Real FPL creator
descriptions run to 500-900 characters of sponsorship links with perhaps one
sentence of content, so the spoken word is the whole point of this source.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Seconds between transcript requests. Slower than feels necessary, because the
# cost of tripping the block is losing the source for hours, while the cost of
# waiting is a few seconds in a job that runs days before a deadline.
FETCH_INTERVAL = 4.0

# Promotional lines that dominate creator descriptions and carry no claims.
BOILERPLATE_MARKERS = (
    "http://",
    "https://",
    "#ad",
    "subscribe",
    "follow me",
    "discord",
    "patreon",
    "twitter",
    "instagram",
    "t&c",
    "use code",
    "sponsor",
)


def strip_boilerplate(text: str) -> str:
    """Remove sponsorship lines from a video description.

    Typically leaves one or two sentences of actual content from several hundred
    characters of links. Worth doing mainly to stop the extractor spending tokens
    on affiliate copy.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("━", "▬", "—", "=")):
            continue
        if any(marker in stripped.lower() for marker in BOILERPLATE_MARKERS):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


# How long a harvested transcript is worth keeping. FPL content is intensely
# time-bound — creators publish several times a week and a Gameweek 5 deadline
# stream is worthless by Gameweek 7 — so the archive turns over fast.
#
# Note what this is and is not for. Stale transcripts already cannot reach a
# forecast: the search only looks back `max_age_days`, so an old video is never
# requested, and the evidence staleness rules expire anything that did get
# through. Retention is about the *archive* — without it a 38-gameweek season
# leaves several hundred dead files committed to the repository forever.
#
# Three weeks rather than two: an international break can leave 14 days between
# gameweeks, and expiring a transcript that is still the most recent word on a
# player would be worse than carrying a few stale files.
DEFAULT_RETENTION_DAYS = 21


def _parse(value: object) -> datetime | None:
    """Parse a stored ISO timestamp, tolerating anything that is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class CacheEntry:
    """A cached transcript, with enough provenance to reason about its age."""

    video_id: str
    text: str | None
    published_at: datetime | None = None
    fetched_at: datetime | None = None
    title: str = ""
    channel: str = ""


@dataclass
class TranscriptCache:
    """On-disk cache of fetched transcripts, with provenance and retention.

    Keyed by video id. Stores misses as well as hits: a video with captions
    disabled will never acquire them, and re-asking every run is exactly the
    behaviour that gets an IP blocked.

    Entries record when the *video* was published, not just when it was fetched.
    Publication is what determines relevance — a transcript pulled this morning
    from a three-week-old video is three weeks stale — and it is what retention
    is measured against.
    """

    directory: Path

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, video_id: str) -> Path:
        # Video ids are URL-safe base64 and can contain '-' and '_', both of
        # which are fine in a filename.
        return self.directory / f"{video_id}.json"

    def entry(self, video_id: str) -> CacheEntry | None:
        """Return the full cached record, or None when absent or unreadable."""
        path = self._path(video_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return CacheEntry(
            video_id=video_id,
            text=payload.get("text"),
            published_at=_parse(payload.get("published_at")),
            fetched_at=_parse(payload.get("fetched_at")),
            title=payload.get("title", ""),
            channel=payload.get("channel", ""),
        )

    def get(self, video_id: str) -> tuple[bool, str | None]:
        """Return ``(cached, text)``. ``cached`` distinguishes a stored miss."""
        found = self.entry(video_id)
        if found is None:
            return False, None
        return True, found.text

    def put(
        self,
        video_id: str,
        text: str | None,
        *,
        published_at: datetime | None = None,
        title: str = "",
        channel: str = "",
    ) -> None:
        payload = {
            "text": text,
            "published_at": published_at.isoformat() if published_at else None,
            "fetched_at": datetime.now(UTC).isoformat(),
            "title": title,
            "channel": channel,
        }
        try:
            self._path(video_id).write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            log.debug("could not cache transcript %s: %s", video_id, exc)

    def age_days(self, video_id: str, *, now: datetime | None = None) -> float | None:
        """Age of a cached transcript in days, or None if it is not cached.

        Falls back through publication date, fetch date, then file mtime. The
        fallbacks matter for entries written before provenance was recorded, and
        they are sound: a transcript is only ever fetched from inside the search
        window, so fetch time is never far from publication time.
        """
        path = self._path(video_id)
        if not path.exists():
            return None
        found = self.entry(video_id)
        reference = (found.published_at or found.fetched_at) if found else None
        if reference is None:
            try:
                reference = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                return None
        return ((now or datetime.now(UTC)) - reference).total_seconds() / 86400

    def ages(self) -> list[tuple[str, float]]:
        """``(video_id, age_in_days)`` for every cached transcript, newest first."""
        now = datetime.now(UTC)
        found = []
        for path in self.directory.glob("*.json"):
            age = self.age_days(path.stem, now=now)
            if age is not None:
                found.append((path.stem, age))
        return sorted(found, key=lambda pair: pair[1])

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[str]:
        """Delete transcripts older than the retention window.

        Returns the ids removed. Deletion is safe by construction: the only cost
        of dropping a transcript that turns out to still be wanted is fetching it
        again, and a video that far outside the search window will not be asked
        for anyway.
        """
        removed = []
        for video_id, age in self.ages():
            if age <= retention_days:
                continue
            try:
                self._path(video_id).unlink()
            except OSError as exc:
                log.debug("could not prune transcript %s: %s", video_id, exc)
                continue
            removed.append(video_id)
        return removed


class TranscriptFetcher:
    """Fetches transcripts, cached and paced, giving up once blocked.

    ``proxy`` routes requests through another IP, which is the only durable fix
    when a block is in force or when running from a cloud provider — whose ranges
    YouTube blocks by default. Any HTTP/HTTPS proxy URL works; a cheap
    residential one is enough for a few dozen requests a week.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        interval: float = FETCH_INTERVAL,
        proxy: str | None = None,
        cache_only: bool = False,
    ) -> None:
        self.cache = TranscriptCache(cache_dir)
        self.interval = interval
        self.proxy = proxy
        # Hosted runners cannot fetch - YouTube blocks their IP ranges - so they
        # read the committed cache and never attempt a request. Trying anyway
        # would waste time and teach YouTube the IP is scraping.
        self.cache_only = cache_only
        self.blocked: str | None = None
        self._last_fetch_at = 0.0

    def fetch(
        self,
        video_id: str,
        *,
        published_at: datetime | None = None,
        title: str = "",
        channel: str = "",
    ) -> str | None:
        """Return a transcript, or None. Never raises.

        The metadata is optional and used only for provenance — it is stored
        beside the text so the cache can later be pruned by video age rather
        than by when this machine happened to run.
        """
        cached, text = self.cache.get(video_id)
        if cached:
            return text

        if self.cache_only:
            return None

        # Once blocked, every further request in this run is refused too, and
        # each one extends the block.
        if self.blocked:
            return None

        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            self.blocked = "youtube-transcript-api is not installed"
            return None

        elapsed = time.monotonic() - self._last_fetch_at
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_fetch_at = time.monotonic()

        try:
            api = YouTubeTranscriptApi(proxy_config=_proxy_config(self.proxy))
            fetched = api.fetch(video_id, languages=["en", "en-GB", "en-US"])
        except Exception as exc:  # the library raises many distinct types
            kind = type(exc).__name__
            if "IpBlocked" in kind or "TooManyRequests" in kind:
                self.blocked = (
                    "YouTube has rate-limited this IP. Cached transcripts still "
                    "work and new ones resume when the block lifts (usually "
                    "hours). To avoid it entirely, set research.transcript_proxy "
                    "in config.yaml — the block is per IP, and no client-side "
                    "approach avoids it."
                )
                return None
            # No captions on this video. Cache the miss so it is never re-asked.
            self.cache.put(video_id, None, published_at=published_at, title=title)
            log.debug("no transcript for %s: %s", video_id, kind)
            return None

        text = " ".join(chunk.text for chunk in fetched)
        self.cache.put(video_id, text, published_at=published_at, title=title, channel=channel)
        return text

    @property
    def cached_count(self) -> int:
        return len(list(self.cache.directory.glob("*.json")))

    @property
    def freshest(self) -> float | None:
        """Age in hours of the newest cached video, if any.

        The scheduled run reports this. A cache last topped up two weeks ago is
        still worth reading, but you should know it is two weeks old rather than
        assume it is current — especially on a hosted run, where the cache is
        whatever the last local harvest happened to commit.
        """
        ages = self.cache.ages()
        return ages[0][1] * 24 if ages else None

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[str]:
        """Drop transcripts that have aged out. See :meth:`TranscriptCache.prune`."""
        return self.cache.prune(retention_days)


def _proxy_config(proxy: str | None):
    """Build the library's proxy config, or None.

    Returns None on any import problem rather than raising: a missing proxy
    class should cost the proxy, not the whole transcript path.
    """
    if not proxy:
        return None
    try:
        from youtube_transcript_api.proxies import GenericProxyConfig
    except ImportError:
        log.warning("installed youtube-transcript-api has no proxy support")
        return None
    return GenericProxyConfig(http_url=proxy, https_url=proxy)

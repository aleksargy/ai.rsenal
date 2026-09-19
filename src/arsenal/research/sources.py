"""Source adapters.

Every adapter returns ``list[Evidence]`` and **degrades to empty rather than
raising**. One dead scraper must never cost a gameweek — a partial forecast beats
a missed deadline, and the run reports what it lost rather than pretending it had
everything.

Adapters that need credentials return empty when those are absent, which makes an
unconfigured source indistinguishable in behaviour from a failing one. That is
deliberate: both mean "no evidence from here", and both are reported.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from ..fpl.schemas import Bootstrap, Element
from .evidence import Evidence, Tier
from .resolver import PlayerResolver

log = logging.getLogger(__name__)

USER_AGENT = "ai.rsenal/0.1 (personal FPL assistant)"


@dataclass
class SourceResult:
    """What one adapter produced, including how it failed if it did."""

    name: str
    evidence: list[Evidence] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    note: str | None = None

    @classmethod
    def failed(cls, name: str, error: str) -> SourceResult:
        return cls(name=name, ok=False, error=error)

    @classmethod
    def skipped(cls, name: str, reason: str) -> SourceResult:
        return cls(name=name, ok=True, note=reason)


class Source(ABC):
    """One place evidence comes from."""

    name: str

    @abstractmethod
    def gather(self, resolver: PlayerResolver, **context: object) -> SourceResult:
        """Collect evidence. Must not raise — return a failed result instead."""

    def safe_gather(self, resolver: PlayerResolver, **context: object) -> SourceResult:
        """Run :meth:`gather` with a hard guarantee it cannot raise."""
        try:
            return self.gather(resolver, **context)
        except Exception as exc:  # catching everything is the point of this wrapper
            log.warning("source %s failed: %s", self.name, exc, exc_info=True)
            return SourceResult.failed(self.name, str(exc))


class FPLNewsSource(Source):
    """Tier 1 availability straight from the FPL API.

    ``status``, ``chance_of_playing_next_round`` and ``news`` are club-sourced,
    which makes them the strongest evidence available anywhere — and they are
    free, structured, and need no scraping. A surprising amount of what people
    build scrapers for is already sitting in ``bootstrap-static``.
    """

    name = "fpl-news"

    def __init__(self, bootstrap: Bootstrap) -> None:
        self.bootstrap = bootstrap

    def gather(self, resolver: PlayerResolver, **context: object) -> SourceResult:
        now = datetime.now(UTC)
        evidence: list[Evidence] = []

        for element in self.bootstrap.elements:
            if not element.news and element.status == "a":
                continue
            claim = self._claim_for(element)
            if claim is None:
                continue
            if element.news_added is not None:
                claim = f"{claim} (announced {element.news_added:%d %b})"
            evidence.append(
                Evidence(
                    player_id=element.id,
                    team_id=element.team,
                    claim=claim,
                    tier=Tier.FACT,
                    impact="availability",
                    source_url=f"fpl://element/{element.id}",
                    source_name="FPL official player status",
                    # `status` is a LIVE STATE FIELD, not a dated report: it
                    # describes the player right now. Timestamping it with
                    # `news_added` made every long-term injury look stale and the
                    # staleness rule discarded it — so a player out since August
                    # with "unknown return date" silently stopped counting as
                    # injured. The observation is current; the announcement date
                    # is preserved in the claim text instead.
                    published_at=now,
                    confidence=1.0,
                    hedged=element.status == "d",
                )
            )

        return SourceResult(name=self.name, evidence=evidence)

    @staticmethod
    def _claim_for(element: Element) -> str | None:
        chance = element.chance_of_playing_next_round
        news = element.news.strip()
        match element.status:
            case "i":
                return f"Injured: {news or 'no detail given'}"
            case "s":
                return f"Suspended: {news or 'no detail given'}"
            case "u":
                return f"Unavailable or has left the league: {news or 'no detail given'}"
            case "n":
                return f"Not eligible to play: {news or 'no detail given'}"
            case "d":
                pct = f"{chance}% chance of playing" if chance is not None else "doubtful"
                return f"Doubtful ({pct}): {news or 'no detail given'}"
            case _:
                return f"Club note: {news}" if news else None


class SetPieceSource(Source):
    """Tier 1 set-piece and penalty duty.

    Penalty order is one of the most undervalued signals in FPL — a first-choice
    penalty taker is worth materially more than his open-play xG suggests, and it
    is a role that changes quietly mid-season.
    """

    name = "set-pieces"

    def __init__(self, bootstrap: Bootstrap) -> None:
        self.bootstrap = bootstrap

    def gather(self, resolver: PlayerResolver, **context: object) -> SourceResult:
        now = datetime.now(UTC)
        evidence = [
            Evidence(
                player_id=element.id,
                team_id=element.team,
                claim=self._duties(element),
                tier=Tier.FACT,
                impact="set_pieces",
                source_url=f"fpl://element/{element.id}/set-pieces",
                source_name="FPL set-piece order",
                published_at=now,
                confidence=1.0,
            )
            for element in self.bootstrap.elements
            if element.penalties_order is not None
            or element.direct_freekicks_order is not None
            or element.corners_and_indirect_freekicks_order is not None
        ]
        return SourceResult(name=self.name, evidence=evidence)

    @staticmethod
    def _duties(element: Element) -> str:
        parts = []
        if element.penalties_order is not None:
            parts.append(f"penalties #{element.penalties_order}")
        if element.direct_freekicks_order is not None:
            parts.append(f"direct free kicks #{element.direct_freekicks_order}")
        if element.corners_and_indirect_freekicks_order is not None:
            parts.append(f"corners #{element.corners_and_indirect_freekicks_order}")
        return "Set-piece duty: " + ", ".join(parts)


class RedditSource(Source):
    """Tier 3-4 community signal from r/FantasyPL.

    Scout threads and daily discussion are the fastest route to late team news —
    frequently the single highest-value input in the final hours before a
    deadline, because it is the one thing statistics cannot supply.

    Raw posts are returned here as *unextracted text*. The LLM extractor turns
    them into claims, and the tier rules decide whether any of it may move a
    number.
    """

    name = "reddit"

    def __init__(self, subreddits: list[str], *, limit: int = 25) -> None:
        self.subreddits = subreddits
        self.limit = limit

    def gather(self, resolver: PlayerResolver, **context: object) -> SourceResult:
        documents: list[tuple[str, str, datetime]] = []
        # Reddit's public JSON needs no credentials for read-only listings, but
        # it does require an honest User-Agent — a default client string is
        # rate-limited almost immediately.
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
            for subreddit in self.subreddits:
                url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={self.limit}"
                response = client.get(url)
                if response.status_code != 200:
                    return SourceResult.failed(
                        self.name, f"r/{subreddit} returned {response.status_code}"
                    )
                for child in response.json().get("data", {}).get("children", []):
                    data = child.get("data", {})
                    text = f"{data.get('title', '')}\n{data.get('selftext', '')}".strip()
                    if not text:
                        continue
                    documents.append(
                        (
                            text[:4000],
                            f"https://www.reddit.com{data.get('permalink', '')}",
                            datetime.fromtimestamp(data.get("created_utc", 0), tz=UTC),
                        )
                    )

        return SourceResult(
            name=self.name,
            evidence=[],
            note=f"{len(documents)} documents fetched for extraction",
        )


@dataclass
class Document:
    """Unstructured text awaiting claim extraction."""

    text: str
    url: str
    source_name: str
    published_at: datetime
    tier: Tier


def fetch_reddit_documents(
    subreddits: list[str], *, limit: int = 25, max_age_days: int = 5
) -> tuple[list[Document], str | None]:
    """Fetch recent r/FantasyPL posts as documents for extraction.

    Returns ``(documents, error)`` — never raises, so a Reddit outage costs this
    source and nothing else.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    documents: list[Document] = []
    try:
        headers = {"User-Agent": USER_AGENT}
        with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
            for subreddit in subreddits:
                response = client.get(
                    f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
                )
                response.raise_for_status()
                for child in response.json().get("data", {}).get("children", []):
                    data = child.get("data", {})
                    published = datetime.fromtimestamp(data.get("created_utc", 0), tz=UTC)
                    if published < cutoff:
                        continue
                    text = f"{data.get('title', '')}\n\n{data.get('selftext', '')}".strip()
                    if len(text) < 40:
                        continue
                    documents.append(
                        Document(
                            text=text[:6000],
                            url=f"https://www.reddit.com{data.get('permalink', '')}",
                            source_name=f"r/{subreddit}",
                            published_at=published,
                            tier=Tier.OPINION,
                        )
                    )
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("reddit fetch failed: %s", exc)
        return documents, str(exc)
    return documents, None


def fetch_youtube_documents(
    api_key: str | None,
    channel_ids: list[str],
    *,
    max_age_days: int = 5,
    per_channel: int = 3,
) -> tuple[list[Document], str | None]:
    """Fetch recent creator video descriptions for extraction.

    Without an API key this returns empty, which is the correct degradation: the
    pipeline runs without creator input rather than failing.

    A caveat worth knowing before wiring transcripts in: creator content is
    **stale on arrival** — a Monday video is frequently obsolete by Friday's
    press conference — and creators are rewarded for bold calls rather than for
    calibration. This is Tier 4, and the tier rules will not let it move a number
    on its own.
    """
    if not api_key or not channel_ids:
        return [], None

    published_after = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    documents: list[Document] = []
    try:
        with httpx.Client(timeout=20.0) as client:
            for channel_id in channel_ids:
                response = client.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={
                        "key": api_key,
                        "channelId": channel_id,
                        "part": "snippet",
                        "order": "date",
                        "maxResults": per_channel,
                        "type": "video",
                        "publishedAfter": published_after,
                    },
                )
                response.raise_for_status()
                for item in response.json().get("items", []):
                    snippet = item.get("snippet", {})
                    video_id = item.get("id", {}).get("videoId")
                    if not video_id:
                        continue
                    text = f"{snippet.get('title', '')}\n\n{snippet.get('description', '')}"
                    documents.append(
                        Document(
                            text=text[:6000],
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            source_name=snippet.get("channelTitle", "YouTube"),
                            published_at=datetime.fromisoformat(
                                snippet["publishedAt"].replace("Z", "+00:00")
                            ),
                            tier=Tier.OPINION,
                        )
                    )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("youtube fetch failed: %s", exc)
        return documents, str(exc)
    return documents, None

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

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

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


@dataclass
class Document:
    """Unstructured text awaiting claim extraction."""

    text: str
    url: str
    source_name: str
    published_at: datetime
    tier: Tier

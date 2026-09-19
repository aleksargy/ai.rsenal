"""Per-player, per-gameweek match history.

Sourced from ``/api/event/{gw}/live/``, which returns every player's stats for a
gameweek in a single request. Building history this way costs one request per
completed gameweek — around four — rather than one per player, which would be
several hundred. It is also the same data the engine scored from, so it is exact
rather than reconstructed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..fpl.client import FPLClient
from ..fpl.schemas import Bootstrap, Position

log = logging.getLogger(__name__)

# Defensive-action counts that earn the flat 2 points, by position. Verified
# empirically against the engine's own `explain` breakdown — see the `fpl-rules`
# skill. Goalkeepers are ineligible.
DC_THRESHOLD: dict[Position, int | None] = {
    Position.GKP: None,
    Position.DEF: 10,
    Position.MID: 12,
    Position.FWD: 12,
}

# A "full" appearance for rate purposes, and the point at which the second
# appearance point and clean sheets become available.
SIXTY = 60


def _number(value: Any) -> float:
    """Coerce an API value to float.

    Several numeric fields arrive as strings (``expected_goals`` is ``"0.42"``),
    and occasionally as null. Everything downstream divides by these, so a silent
    ``None`` would poison a rate rather than fail.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class PlayerGameweek:
    """One player's record for one gameweek."""

    element_id: int
    gameweek: int
    minutes: int
    started: bool
    goals: int
    assists: int
    clean_sheet: bool
    goals_conceded: int
    saves: int
    bonus: int
    bps: int
    yellow_cards: int
    red_cards: int
    defensive_actions: int
    """The raw count, not points. The threshold in ``DC_THRESHOLD`` applies to it."""

    xg: float
    xa: float
    xgc: float
    """Team expected goals conceded in this match. Identical for every player in
    the same team, so it doubles as a team-level defensive record."""

    total_points: int

    @property
    def played(self) -> bool:
        return self.minutes > 0

    @property
    def played_sixty(self) -> bool:
        return self.minutes >= SIXTY

    def earned_defensive_contribution(self, position: Position) -> bool:
        threshold = DC_THRESHOLD[position]
        return threshold is not None and self.defensive_actions >= threshold


@dataclass
class History:
    """Every completed gameweek, indexed for the access patterns the model needs."""

    records: list[PlayerGameweek]
    gameweeks: list[int]

    def by_player(self) -> dict[int, list[PlayerGameweek]]:
        out: dict[int, list[PlayerGameweek]] = {}
        for record in self.records:
            out.setdefault(record.element_id, []).append(record)
        for entries in out.values():
            entries.sort(key=lambda r: r.gameweek)
        return out

    def for_player(self, element_id: int) -> list[PlayerGameweek]:
        return sorted(
            (r for r in self.records if r.element_id == element_id),
            key=lambda r: r.gameweek,
        )

    @property
    def latest_gameweek(self) -> int:
        return max(self.gameweeks, default=0)


def _parse_live(payload: dict[str, Any], gameweek: int) -> Iterable[PlayerGameweek]:
    for entry in payload.get("elements", []):
        stats = entry.get("stats") or {}
        # Players who did not feature carry a full row of zeroes. Keeping them
        # would drag every rate toward zero and, worse, make "did not play" and
        # "played badly" indistinguishable. They are excluded here; availability
        # is modelled separately from the squad each week.
        minutes = int(stats.get("minutes") or 0)
        if minutes <= 0:
            continue
        yield PlayerGameweek(
            element_id=int(entry["id"]),
            gameweek=gameweek,
            minutes=minutes,
            started=bool(stats.get("starts")),
            goals=int(stats.get("goals_scored") or 0),
            assists=int(stats.get("assists") or 0),
            clean_sheet=bool(stats.get("clean_sheets")),
            goals_conceded=int(stats.get("goals_conceded") or 0),
            saves=int(stats.get("saves") or 0),
            bonus=int(stats.get("bonus") or 0),
            bps=int(stats.get("bps") or 0),
            yellow_cards=int(stats.get("yellow_cards") or 0),
            red_cards=int(stats.get("red_cards") or 0),
            defensive_actions=int(stats.get("defensive_contribution") or 0),
            xg=_number(stats.get("expected_goals")),
            xa=_number(stats.get("expected_assists")),
            xgc=_number(stats.get("expected_goals_conceded")),
            total_points=int(stats.get("total_points") or 0),
        )


def load_history(
    client: FPLClient,
    bootstrap: Bootstrap,
    *,
    up_to: int | None = None,
    ttl: int | None = None,
) -> History:
    """Load every completed gameweek up to and including ``up_to``.

    Finished gameweeks are immutable, so they cache indefinitely. The current
    gameweek is deliberately excluded until ``data_checked`` — bonus points are
    provisional until then, and a half-scored gameweek would bias every rate.
    """
    completed = [
        event.id
        for event in bootstrap.events
        if event.finished and event.data_checked and (up_to is None or event.id <= up_to)
    ]

    records: list[PlayerGameweek] = []
    for gameweek in sorted(completed):
        payload = client.event_live(gameweek, ttl=ttl)
        records.extend(_parse_live(payload, gameweek))

    log.info("loaded %d player-gameweeks across GW%s", len(records), completed)
    return History(records=records, gameweeks=sorted(completed))

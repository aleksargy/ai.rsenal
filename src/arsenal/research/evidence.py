"""The Evidence record, and the tier rules that govern what it may do.

Every claim entering the pipeline becomes one of these. Untraceable claims are
discarded rather than downweighted — a model-generated recollection is not
evidence, and no confidence score makes it one.

The tier system is enforced here in code rather than left to documentation,
because the single most damaging failure mode of a system like this is a
confident YouTuber's "nailed on to start" moving a forecast on its own. See
:func:`may_adjust_forecast`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Tier(IntEnum):
    """How close a claim sits to ground truth. Lower is stronger."""

    FACT = 1
    """Official FPL API, completed match data, confirmed lineups, club statements."""

    MEASURED = 2
    """Understat / FBref / Opta underlying numbers. Objective, needs interpretation."""

    REPORTED = 3
    """Press conferences, named beat reporters, editorial team-news pages."""

    OPINION = 4
    """Creators, Reddit, blog predictions. Hypotheses to verify — never evidence."""


Impact = Literal[
    "availability",
    "minutes",
    "role",
    "set_pieces",
    "form",
    "fixture",
    "price",
]

# Tier 4 exists to surface claims worth checking and to read what the field is
# doing. It must never move a number on its own: five creators repeating one
# rumour is one unverified rumour, not five, and popularity is uncorrelated with
# accuracy in engagement-driven media.
MAX_TIER_THAT_MOVES_A_FORECAST = Tier.REPORTED

# Availability claims decay fast — a Tuesday injury report is superseded
# entirely by Friday's press conference.
DEFAULT_MAX_AGE = timedelta(days=10)

# Impacts where staleness is decisive rather than merely unhelpful.
FAST_DECAYING: frozenset[str] = frozenset({"availability", "minutes", "role"})


class Evidence(BaseModel):
    """One sourced, timestamped, single-claim assertion."""

    model_config = ConfigDict(frozen=True)

    claim: str = Field(min_length=3)
    """One factual assertion. "Saka is fit and takes penalties" is two records
    with different verification paths and different impacts."""

    tier: Tier
    impact: Impact
    source_url: str
    source_name: str
    published_at: datetime
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    player_id: int | None = None
    team_id: int | None = None

    gameweek: int | None = None
    """The gameweek this claim applies to, when it applies to only one.

    Some constraints are week-specific rather than ongoing — a loan-ineligible
    player cannot face their parent club in one particular gameweek and is
    perfectly available in every other. Applying such a claim to the whole
    horizon would wrongly bench a fit player for a month.
    """

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    """The extractor's own certainty that the claim is correctly *read* from the
    source. Not a probability the claim is true — tier carries that."""

    hedged: bool = False
    """Whether the source hedged. "Should be available" is not "is available",
    and flattening the hedge manufactures confidence the source never had."""

    @field_validator("source_url")
    @classmethod
    def _must_be_citable(cls, value: str) -> str:
        """A claim you cannot cite is a claim you cannot use."""
        if not value.startswith(("http://", "https://", "fpl://")):
            raise ValueError(
                "source_url must be a resolvable URL. Claims without a citable "
                "source are discarded, not downweighted."
            )
        return value

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """Naive timestamps silently compare wrong against a UTC deadline."""
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (use UTC)")
        return value.astimezone(UTC)

    def age(self, *, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(UTC)) - self.published_at

    def applies_to(self, gameweek: int | None) -> bool:
        """Whether this claim bears on the given gameweek.

        A claim with no gameweek is ongoing and applies to all of them.
        """
        if self.gameweek is None or gameweek is None:
            return True
        return self.gameweek == gameweek

    def is_stale(
        self, *, now: datetime | None = None, max_age: timedelta | None = None
    ) -> bool:
        """Whether this claim is too old to act on.

        Fast-decaying impacts use the tight window; the rest are judged on double
        it, since a set-piece or form observation ages far more gracefully than
        an injury report.
        """
        # A claim scoped to a specific future gameweek describes a scheduled
        # fact, not a perishable report. "Ineligible in GW27" does not decay.
        if self.gameweek is not None:
            return False
        limit = max_age or DEFAULT_MAX_AGE
        if self.impact not in FAST_DECAYING:
            limit *= 2
        return self.age(now=now) > limit

    @property
    def may_move_forecast(self) -> bool:
        """Whether this record is permitted to change a number."""
        return self.tier <= MAX_TIER_THAT_MOVES_A_FORECAST


def may_adjust_forecast(evidence: Evidence, *, now: datetime | None = None) -> bool:
    """The gate every adjustment must pass.

    Three independent reasons to refuse, all of them load-bearing:

    * **Tier 4 never moves a number.** It generates hypotheses and reads the
      field. That is its whole job.
    * **Stale claims are discarded**, not faded. A ten-day-old fitness report has
      been superseded by events you simply have not fetched.
    * **A claim with no player and no team cannot be applied** to anything.
    """
    if not evidence.may_move_forecast:
        return False
    if evidence.is_stale(now=now):
        return False
    return evidence.player_id is not None or evidence.team_id is not None


def deduplicate(evidence: list[Evidence]) -> list[Evidence]:
    """Collapse records asserting the same thing about the same player.

    Five outlets reporting one press conference is **one** piece of evidence.
    Counting it five times manufactures a consensus that does not exist, which is
    exactly how an unverified rumour comes to look like a settled fact.

    The strongest tier wins; ties break on recency.
    """
    best: dict[tuple[int | None, str, str], Evidence] = {}
    for item in evidence:
        key = (item.player_id, item.impact, item.claim.strip().lower())
        incumbent = best.get(key)
        if (
            incumbent is None
            or item.tier < incumbent.tier
            or (item.tier == incumbent.tier and item.published_at > incumbent.published_at)
        ):
            best[key] = item
    return sorted(best.values(), key=lambda e: (e.tier, -e.published_at.timestamp()))


def conflicts(evidence: list[Evidence]) -> list[tuple[Evidence, Evidence]]:
    """Find same-tier disagreements about one player's availability.

    Conflict is **reported, never silently resolved**. If two beat reporters
    disagree on whether a player trained, that disagreement *is* the finding: it
    should widen the uncertainty band, and it may itself be reason enough to
    avoid the player rather than to guess which reporter was right.
    """
    found: list[tuple[Evidence, Evidence]] = []
    by_player: dict[int, list[Evidence]] = {}
    for item in evidence:
        if item.player_id is not None and item.impact in FAST_DECAYING:
            by_player.setdefault(item.player_id, []).append(item)

    for records in by_player.values():
        for i, first in enumerate(records):
            for second in records[i + 1 :]:
                if first.tier != second.tier:
                    continue
                if first.hedged != second.hedged and first.impact == second.impact:
                    found.append((first, second))
    return found

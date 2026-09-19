"""Applying evidence to a forecast.

This is the seam where research becomes numbers, and it is where the tier rules
are enforced rather than merely documented. Three properties matter:

* **Tier 4 cannot move a number.** Not "moves it a little" — cannot. It surfaces
  hypotheses and reads the field, and that is all.
* **Adjustments are bounded.** No single claim may swing a player's availability
  arbitrarily, because the extractor is a language model reading noisy prose and
  will sometimes be wrong.
* **Conflicts widen uncertainty rather than picking a winner.** If two reporters
  disagree about a player's fitness, the honest response is less confidence, not
  a coin flip.

The direction of travel is deliberate: evidence mostly *reduces* confidence in a
player. Statistics already know who has been playing; what they cannot know is
that today's press conference ruled someone out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..forecast.minutes import MinutesForecast
from ..forecast.model import PlayerForecast
from .evidence import Evidence, Tier, conflicts, may_adjust_forecast

log = logging.getLogger(__name__)

# How much a single claim of each tier may scale a player's availability.
# Tier 1 is club-sourced and can rule a player out entirely; Tier 3 is a
# reporter's word and is deliberately capped well short of that.
TIER_AUTHORITY: dict[Tier, float] = {
    Tier.FACT: 1.00,
    Tier.MEASURED: 0.60,
    Tier.REPORTED: 0.45,
    Tier.OPINION: 0.00,
}

# Floor on any downgrade from a single piece of evidence, so one misread
# sentence cannot zero out a player the statistics say is nailed.
#
# It applies to Tier 2-3 only. Those tiers reach us through a language model
# reading noisy prose, which is what the floor is defending against. Tier 1 is
# read mechanically from the FPL API itself — when it says a player is suspended
# until 17 October, that is not an interpretation that might be wrong, and
# holding him at 25% availability would let the optimiser field him.
MIN_AVAILABILITY_MULTIPLIER = 0.25

# A hedged claim ("should be fit") carries roughly half the force of an
# unhedged one — which is the entire reason the hedge is preserved upstream.
HEDGE_DISCOUNT = 0.5

# Words that signal a player is ruled out rather than merely doubtful.
RULING_OUT = ("injured", "suspended", "ruled out", "unavailable", "will miss", "out for")
RETURNING = ("fit", "available", "returned", "trained", "back in", "expected to start")

# Extra uncertainty added when sources disagree about the same player.
CONFLICT_SIGMA_PENALTY = 1.5


@dataclass
class Adjustment:
    """What evidence did to one player, and why."""

    player_id: int
    availability_multiplier: float = 1.0
    sigma_multiplier: float = 1.0
    reasons: list[str] = field(default_factory=list)
    applied: list[Evidence] = field(default_factory=list)
    ignored: list[Evidence] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.availability_multiplier != 1.0 or self.sigma_multiplier != 1.0


@dataclass
class ResearchReport:
    """The outcome of applying a body of evidence."""

    adjustments: dict[int, Adjustment] = field(default_factory=dict)
    conflicts: list[tuple[Evidence, Evidence]] = field(default_factory=list)
    hypotheses: list[Evidence] = field(default_factory=list)
    """Tier 4 claims worth verifying at a higher tier. These moved nothing."""

    stale_dropped: int = 0

    out_of_scope: int = 0
    """Claims that are true, but about a different gameweek."""

    @property
    def changed_players(self) -> list[Adjustment]:
        return [a for a in self.adjustments.values() if a.changed]


def _direction(evidence: Evidence) -> float:
    """How far this claim pushes availability, before tier and hedge weighting.

    Negative rules a player out, positive reinstates one, zero is neutral. Read
    from the claim text, which the extractor wrote as a plain assertion.
    """
    text = evidence.claim.lower()
    if any(word in text for word in RULING_OUT):
        return -1.0
    if any(word in text for word in RETURNING):
        return 0.4
    if "doubtful" in text or "%" in text:
        return -0.5
    return 0.0


def _multiplier_for(evidence: Evidence) -> float:
    """The availability multiplier a single claim implies."""
    authority = TIER_AUTHORITY[evidence.tier]
    if authority == 0.0:
        return 1.0

    direction = _direction(evidence)
    if direction == 0.0:
        return 1.0

    force = authority * evidence.confidence
    if evidence.hedged:
        force *= HEDGE_DISCOUNT

    if direction < 0:
        raw = 1.0 + direction * force
        if evidence.tier == Tier.FACT:
            return max(0.0, raw)
        return max(MIN_AVAILABILITY_MULTIPLIER, raw)
    # Positive news cannot push a player above certainty.
    return min(1.0, 1.0 + direction * force * 0.5)


def build_report(
    evidence: list[Evidence],
    *,
    now: datetime | None = None,
    gameweek: int | None = None,
) -> ResearchReport:
    """Turn evidence into per-player adjustments, enforcing the tier rules.

    ``gameweek`` scopes week-specific claims. Without it, a loan-ineligible
    player barred from one fixture in GW27 is treated as unavailable *every*
    week — which silently benches a fit player for the rest of the season.
    """
    moment = now or datetime.now(UTC)
    report = ResearchReport()

    for item in evidence:
        if item.player_id is None:
            continue

        if not item.applies_to(gameweek):
            # Correct for a different gameweek, irrelevant to this one.
            report.out_of_scope += 1
            continue

        adjustment = report.adjustments.setdefault(
            item.player_id, Adjustment(player_id=item.player_id)
        )

        if not may_adjust_forecast(item, now=moment):
            adjustment.ignored.append(item)
            if item.tier == Tier.OPINION:
                # Not evidence, but worth checking at a higher tier. This is the
                # main value Tier 4 provides.
                report.hypotheses.append(item)
            elif item.is_stale(now=moment):
                report.stale_dropped += 1
            continue

        if item.impact not in ("availability", "minutes", "role"):
            # Set-piece, form and fixture evidence is real but is already
            # reflected in the statistical model; applying it again here would
            # double-count it.
            adjustment.applied.append(item)
            continue

        multiplier = _multiplier_for(item)
        if multiplier == 1.0:
            adjustment.applied.append(item)
            continue

        adjustment.availability_multiplier *= multiplier
        adjustment.applied.append(item)
        adjustment.reasons.append(
            f"[T{int(item.tier)}] {item.claim} "
            f"({'hedged, ' if item.hedged else ''}x{multiplier:.2f}) — {item.source_name}"
        )

    # Disagreement is a finding in its own right. It does not resolve to a
    # winner; it makes the player less predictable, which the optimiser's risk
    # adjustment will then price in.
    report.conflicts = conflicts(evidence)
    for first, second in report.conflicts:
        if first.player_id is None:
            continue
        adjustment = report.adjustments.setdefault(
            first.player_id, Adjustment(player_id=first.player_id)
        )
        adjustment.sigma_multiplier *= CONFLICT_SIGMA_PENALTY
        adjustment.reasons.append(
            f"sources disagree: '{first.claim}' ({first.source_name}) vs "
            f"'{second.claim}' ({second.source_name}) — uncertainty widened"
        )

    for adjustment in report.adjustments.values():
        adjustment.availability_multiplier = round(
            max(0.0, adjustment.availability_multiplier), 4
        )

    return report


def apply_to_forecasts(
    forecasts: dict[int, PlayerForecast], report: ResearchReport
) -> dict[int, PlayerForecast]:
    """Rescale forecasts by the evidence-derived adjustments.

    Availability scales **every** points component, because a player who does not
    play scores none of them. It is applied to the minutes forecast rather than
    to the total so the component breakdown stays internally consistent and the
    reasoning remains explainable.
    """
    for player_id, adjustment in report.adjustments.items():
        forecast = forecasts.get(player_id)
        if forecast is None or not adjustment.changed:
            continue

        factor = adjustment.availability_multiplier
        if factor != 1.0:
            forecast.minutes = [
                MinutesForecast(
                    p_appear=round(m.p_appear * factor, 4),
                    p_sixty=round(m.p_sixty * factor, 4),
                    expected_minutes=round(m.expected_minutes * factor, 2),
                )
                for m in forecast.minutes
            ]
            forecast.breakdowns = [
                type(breakdown)(
                    appearance=round(breakdown.appearance * factor, 3),
                    goals=round(breakdown.goals * factor, 3),
                    assists=round(breakdown.assists * factor, 3),
                    clean_sheet=round(breakdown.clean_sheet * factor, 3),
                    goals_conceded=round(breakdown.goals_conceded * factor, 3),
                    defensive_contribution=round(breakdown.defensive_contribution * factor, 3),
                    saves=round(breakdown.saves * factor, 3),
                    bonus=round(breakdown.bonus * factor, 3),
                    cards=round(breakdown.cards * factor, 3),
                )
                for breakdown in forecast.breakdowns
            ]

    return forecasts

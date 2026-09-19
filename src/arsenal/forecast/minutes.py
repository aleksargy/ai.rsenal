"""Appearance and minutes modelling.

This is the highest-leverage part of the forecast. A 12-point player at 50% to
start is worth less than a 7-point nailed starter, so an error here swamps any
amount of precision in the attacking model.

Two probabilities are modelled **separately**, because they carry different
points and have very different distributions for rotation risks:

* ``p_appear`` — any minutes at all. Worth 1 point, and gates every other return.
* ``p_sixty`` — sixty minutes or more. Worth the second appearance point, and is
  the precondition for a clean sheet.

Collapsing them into a single "will he play" number overvalues rotation risks and
cameo substitutes, who appear often and reach sixty minutes rarely.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..fpl.schemas import Element
from .history import PlayerGameweek
from .teams import shrink

# Recency decay per gameweek. Last week's role tells you far more about this week
# than a start in August does.
RECENCY_DECAY = 0.8

# Gameweeks of prior weight when shrinking a player's rate.
#
# This has to stay well under the effective sample size or it swamps the
# evidence. Note that the recency decay *reduces* that size: four gameweeks at a
# 0.8 decay are worth 2.95, not 4. At a prior weight of 2.5 a player who had
# started every match came out at 70% to play, which would have systematically
# undervalued exactly the nailed starters you most want to own.
PRIOR_GAMEWEEKS = 1.2

# Two different priors, for two different jobs.
#
# PRIOR_* is for a player with **no history at all** and is deliberately
# pessimistic: an unknown player is far more often a fringe squad member than a
# starter, and overrating one wastes a transfer.
PRIOR_P_APPEAR = 0.35
PRIOR_P_SIXTY = 0.25

# SHRINK_TARGET_* is what an *observed* rate is pulled toward — roughly the
# population mean among players who feature at all. Using the pessimistic prior
# here instead would drag every established starter down toward a fringe player's
# rate, which is not what shrinkage is for.
SHRINK_TARGET_APPEAR = 0.55
SHRINK_TARGET_SIXTY = 0.45

# Typical minutes conditional on the outcome, used to convert probabilities into
# expected minutes.
MINUTES_IF_SIXTY = 84.0
MINUTES_IF_CAMEO = 25.0


@dataclass(frozen=True)
class MinutesForecast:
    p_appear: float
    p_sixty: float
    expected_minutes: float

    @property
    def appearance_points(self) -> float:
        """Expected appearance points: 1 for playing, 2 for sixty minutes or more."""
        return self.p_appear + self.p_sixty


def availability(element: Element) -> float:
    """Club-sourced probability the player is fit and selectable.

    ``status`` and ``chance_of_playing_next_round`` come from the club, which
    makes them Tier 1 — better than any amount of inference from past minutes.
    This is a *fitness* signal only; whether a fit player actually starts is the
    history's job.
    """
    if not element.is_transactable or element.status in ("u", "s"):
        return 0.0
    chance = element.chance_of_playing_next_round
    if chance is not None:
        return chance / 100.0
    if element.status == "i":
        return 0.0
    if element.status == "d":
        return 0.5
    if element.status == "n":
        return 0.0
    return 1.0


def forecast_minutes(
    element: Element,
    records: list[PlayerGameweek],
    completed_gameweeks: list[int],
    *,
    use_availability: bool = True,
) -> MinutesForecast:
    """Combine historical role with current fitness.

    The denominator is every completed gameweek, not merely those the player
    featured in — otherwise a player who appeared once in five weeks would score
    a perfect appearance rate. Absences are evidence, and dropping them is how a
    rotation risk comes to look nailed.

    ``use_availability=False`` predicts from history alone. Backtests need it:
    ``status`` describes today, so applying it to a gameweek that has already
    happened tells the model who got injured, which it could not have known then.
    """
    fit = availability(element) if use_availability else 1.0
    if fit <= 0.0:
        return MinutesForecast(p_appear=0.0, p_sixty=0.0, expected_minutes=0.0)

    if not completed_gameweeks:
        return MinutesForecast(
            p_appear=fit * PRIOR_P_APPEAR,
            p_sixty=fit * PRIOR_P_SIXTY,
            expected_minutes=fit * PRIOR_P_SIXTY * MINUTES_IF_SIXTY,
        )

    played = {record.gameweek: record for record in records}
    latest = max(completed_gameweeks)

    weight_total = 0.0
    appeared = 0.0
    sixty = 0.0
    for gameweek in completed_gameweeks:
        weight = RECENCY_DECAY ** (latest - gameweek)
        weight_total += weight
        record = played.get(gameweek)
        if record is None:
            continue
        appeared += weight
        if record.played_sixty:
            sixty += weight

    observed_appear = appeared / weight_total if weight_total else 0.0
    observed_sixty = sixty / weight_total if weight_total else 0.0
    effective_n = weight_total

    p_appear = fit * shrink(observed_appear, SHRINK_TARGET_APPEAR, effective_n, PRIOR_GAMEWEEKS)
    p_sixty = fit * shrink(observed_sixty, SHRINK_TARGET_SIXTY, effective_n, PRIOR_GAMEWEEKS)
    # A player cannot reach sixty minutes more often than he appears at all.
    p_sixty = min(p_sixty, p_appear)

    expected = p_sixty * MINUTES_IF_SIXTY + (p_appear - p_sixty) * MINUTES_IF_CAMEO
    return MinutesForecast(
        p_appear=round(p_appear, 4),
        p_sixty=round(p_sixty, 4),
        expected_minutes=round(expected, 2),
    )

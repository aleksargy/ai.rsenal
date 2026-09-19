"""A placeholder expected-points forecast, and the candidate pool builder.

**The forecast here is deliberately crude and M3 replaces it.** It exists so the
optimiser can be exercised end-to-end today, using only free structured data from
the FPL API and no LLM inference at all. It leans on FPL's own ``ep_next``, which
is a reasonable baseline to *beat* — not a target to copy.

What it does not do, and what M3 must: model appearance and 60-minute
probabilities separately, build attacking returns from npxG and penalty share,
derive clean-sheet probability from opponent strength, model defensive
contribution as a threshold crossing, and regress hot streaks toward a
position-and-price prior. See the ``fpl-research`` skill.
"""

from __future__ import annotations

from ..fpl.schemas import Availability, Bootstrap, Element, MyTeam, Position
from .model import Candidate

# How fast trust in FPL's one-week estimate decays across the horizon. Later
# gameweeks fall back to the season-long scoring rate, which is more stable.
EP_DECAY = 0.7

# Below this many minutes a player's per-game average is mostly noise, so it is
# shrunk toward a replacement-level baseline.
MINUTES_FOR_FULL_CONFIDENCE = 450.0

# Roughly a bench player's return. Used as the shrink target for small samples.
REPLACEMENT_LEVEL_PPG = 2.0


def availability(element: Element) -> float:
    """Probability the player features at all.

    Club-sourced Tier 1 data: ``status`` and ``chance_of_playing_next_round``.
    Crucially this is *not* the same as P(60 minutes), which M3 must model
    separately — they have different point consequences and very different
    distributions for rotation risks.
    """
    if element.status == Availability.UNAVAILABLE or not element.is_transactable:
        return 0.0
    if element.status == Availability.SUSPENDED:
        return 0.0
    chance = element.chance_of_playing_next_round
    if chance is not None:
        return chance / 100.0
    if element.status == Availability.INJURED:
        return 0.0
    if element.status == Availability.DOUBTFUL:
        return 0.5
    return 1.0


def _scoring_rate(element: Element) -> float:
    """Season points per game, shrunk toward replacement level on small samples.

    Five gameweeks is a tiny sample. Without this, a player with one big haul
    from 90 minutes looks like an elite asset.
    """
    confidence = min(1.0, element.minutes / MINUTES_FOR_FULL_CONFIDENCE)
    return confidence * element.points_per_game + (1 - confidence) * REPLACEMENT_LEVEL_PPG


def baseline_xp(element: Element, horizon: int) -> tuple[float, ...]:
    """Expected points per gameweek across the horizon. Placeholder — see module docstring."""
    p_play = availability(element)
    if p_play == 0.0:
        return tuple(0.0 for _ in range(horizon))

    fpl_estimate = float(element.ep_next or 0.0)
    season_rate = _scoring_rate(element)

    points = []
    for t in range(horizon):
        # Near-term trusts FPL's own fixture-aware estimate; later gameweeks
        # regress to the season rate, since no fixture information is used here.
        weight = EP_DECAY**t
        estimate = weight * fpl_estimate + (1 - weight) * season_rate
        points.append(round(p_play * estimate, 3))
    return tuple(points)


def baseline_sigma(element: Element, xp: tuple[float, ...]) -> tuple[float, ...]:
    """A crude uncertainty band.

    Two sources of variance, both real: rotation risk (a player who might not
    play has a bimodal outcome) and the inherent lumpiness of FPL scoring. This
    is a stand-in for a proper predictive distribution, and exists so the
    optimiser's risk adjustment has something to work with.
    """
    p_play = availability(element)
    rotation_risk = 2.0 * p_play * (1 - p_play)  # peaks at a 50/50 starter
    return tuple(round(0.6 * value + rotation_risk, 3) for value in xp)


def build_candidates(
    bootstrap: Bootstrap,
    *,
    my_team: MyTeam | None = None,
    horizon: int = 5,
    per_position: int = 35,
) -> list[Candidate]:
    """Build the optimiser's candidate pool.

    The full 662-player universe makes the model needlessly large and slow, so
    this keeps the strongest ``per_position`` players by expected points plus
    every player currently owned. Owned players are **always** included — the
    continuity constraint is unsatisfiable without them, and their absence
    presents as a confusing infeasibility rather than a missing-data error.
    """
    owned: dict[int, int] = {}
    if my_team is not None:
        owned = {pick.element: pick.purchase_price for pick in my_team.picks}

    scored: list[tuple[float, Candidate]] = []
    for element in bootstrap.elements:
        is_owned = element.id in owned
        if not is_owned and not element.is_transactable:
            continue

        xp = baseline_xp(element, horizon)
        candidate = Candidate(
            element_id=element.id,
            position=element.element_type,
            team=element.team,
            now_cost=element.now_cost,
            xp=xp,
            sigma=baseline_sigma(element, xp),
            owned=is_owned,
            purchase_price=owned.get(element.id),
            name=element.name,
        )
        scored.append((sum(xp), candidate))

    pool: dict[int, Candidate] = {}
    for position in Position:
        ranked = sorted(
            (item for item in scored if item[1].position is position),
            key=lambda item: item[0],
            reverse=True,
        )
        for _, candidate in ranked[:per_position]:
            pool[candidate.element_id] = candidate

    # Owned players are non-negotiable, however badly they are scoring.
    for _, candidate in scored:
        if candidate.owned:
            pool[candidate.element_id] = candidate

    return sorted(pool.values(), key=lambda c: c.element_id)

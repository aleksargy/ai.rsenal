"""Turning forecasts into an optimiser candidate pool.

This is the seam between the two halves of the system. Everything upstream —
history, team strength, minutes, research — exists to produce an expected-points
number per player per gameweek. Everything downstream treats that number as
given and optimises against hard constraints.

Keeping the seam this narrow is deliberate: the solver never sees a player's
name, news, or reputation, only a vector. It cannot be talked into a bad squad.
"""

from __future__ import annotations

from ..forecast.model import PlayerForecast
from ..fpl.schemas import Bootstrap, MyTeam, Position
from .model import Candidate


def candidates_from_forecasts(
    bootstrap: Bootstrap,
    forecasts: dict[int, PlayerForecast],
    *,
    my_team: MyTeam | None = None,
    per_position: int = 35,
) -> list[Candidate]:
    """Build the optimiser pool from expected-points forecasts.

    The full 660-player universe makes the model needlessly large, so this keeps
    the strongest ``per_position`` players plus **every player currently owned**.

    Owned players are non-negotiable regardless of how badly they are forecast:
    the squad-continuity constraint is unsatisfiable without them, and their
    absence surfaces as a bare "no legal squad exists" rather than as the
    missing-data error it actually is.
    """
    owned: dict[int, int] = {}
    if my_team is not None:
        owned = {pick.element: pick.purchase_price for pick in my_team.picks}

    scored: list[tuple[float, Candidate]] = []
    for element in bootstrap.elements:
        forecast = forecasts.get(element.id)
        if forecast is None:
            continue
        is_owned = element.id in owned
        # A player who has left the league cannot be bought, but one already in
        # the squad must still be represented so he can be sold.
        if not is_owned and not element.is_transactable:
            continue

        candidate = Candidate(
            element_id=element.id,
            position=element.element_type,
            team=element.team,
            now_cost=element.now_cost,
            xp=forecast.xp,
            sigma=forecast.sigma,
            owned=is_owned,
            purchase_price=owned.get(element.id),
            name=element.name,
        )
        scored.append((sum(forecast.xp), candidate))

    pool: dict[int, Candidate] = {}
    for position in Position:
        ranked = sorted(
            (item for item in scored if item[1].position is position),
            key=lambda item: item[0],
            reverse=True,
        )
        for _, candidate in ranked[:per_position]:
            pool[candidate.element_id] = candidate

    for _, candidate in scored:
        if candidate.owned:
            pool[candidate.element_id] = candidate

    return sorted(pool.values(), key=lambda c: c.element_id)

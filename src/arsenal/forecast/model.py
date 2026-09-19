"""Bottom-up expected points.

Points are built from their scoring components rather than extrapolated from past
totals, because the components have very different persistence: shot volume is
stable, finishing is not; defensive-action rates are stable, clean sheets depend
on the opponent. Extrapolating last month's points forecasts the noise along with
the signal.

    xP = appearance
       + goals + assists
       + clean sheet + goals-conceded penalty
       + defensive contribution
       + saves
       + bonus
       - cards

Every rate is measured per 90, shrunk toward a positional prior in proportion to
the minutes behind it, then scaled by expected minutes and the fixture.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..fpl.schemas import Bootstrap, Element, Position
from .history import DC_THRESHOLD, History, PlayerGameweek
from .minutes import MinutesForecast, forecast_minutes
from .teams import LeagueModel, TeamFixture, shrink

log = logging.getLogger(__name__)

# Minutes of prior weight when shrinking a per-90 rate. Roughly three matches:
# enough to stop a single big game defining a player, not so much that a genuine
# starter never escapes the prior.
PRIOR_MINUTES = 270.0

# Positional priors for attacking rates, per 90. Deliberately modest — the cost
# of over-regressing a good player is small next to the cost of extrapolating a
# hot streak from 200 minutes.
PRIOR_XG90: dict[Position, float] = {
    Position.GKP: 0.0,
    Position.DEF: 0.05,
    Position.MID: 0.12,
    Position.FWD: 0.28,
}
PRIOR_XA90: dict[Position, float] = {
    Position.GKP: 0.0,
    Position.DEF: 0.06,
    Position.MID: 0.14,
    Position.FWD: 0.12,
}
PRIOR_BONUS90: dict[Position, float] = {
    Position.GKP: 0.20,
    Position.DEF: 0.20,
    Position.MID: 0.22,
    Position.FWD: 0.28,
}
PRIOR_SAVES90 = 2.8
PRIOR_YELLOW90 = 0.15

# Probability of crossing the defensive-action threshold in a started match,
# before any evidence. Defenders clear the 10-action bar reasonably often;
# forwards essentially never reach 12 — over 140 player-gameweeks of GW1-4 data,
# not one did.
PRIOR_P_DC: dict[Position, float] = {
    Position.GKP: 0.0,
    Position.DEF: 0.22,
    Position.MID: 0.08,
    Position.FWD: 0.01,
}
PRIOR_DC_MATCHES = 3.0

DC_POINTS = 2.0
ASSIST_POINTS = 3.0
SAVES_PER_POINT = 3.0
YELLOW_POINTS = -1.0

# Overdispersion factor converting expected points into a variance estimate. FPL
# scoring is lumpy — returns arrive in 4-6 point jumps — so the variance runs
# well above the mean.
RETURN_OVERDISPERSION = 3.0


@dataclass(frozen=True)
class PointsBreakdown:
    """Expected points by component, for one player in one gameweek.

    Kept separately so a recommendation can be explained — "captain him because
    of a 62% clean-sheet chance", not merely "he scores 6.2".
    """

    appearance: float = 0.0
    goals: float = 0.0
    assists: float = 0.0
    clean_sheet: float = 0.0
    goals_conceded: float = 0.0
    defensive_contribution: float = 0.0
    saves: float = 0.0
    bonus: float = 0.0
    cards: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.appearance
            + self.goals
            + self.assists
            + self.clean_sheet
            + self.goals_conceded
            + self.defensive_contribution
            + self.saves
            + self.bonus
            + self.cards,
            3,
        )


@dataclass(frozen=True)
class PlayerRates:
    """Per-90 rates after shrinkage. Fixture-independent."""

    xg90: float
    xa90: float
    bonus90: float
    saves90: float
    yellow90: float
    p_dc: float
    minutes: float


@dataclass
class PlayerForecast:
    element_id: int
    position: Position
    team: int
    name: str
    minutes: list[MinutesForecast] = field(default_factory=list)
    breakdowns: list[PointsBreakdown] = field(default_factory=list)
    rates: PlayerRates | None = None

    @property
    def xp(self) -> tuple[float, ...]:
        return tuple(b.total for b in self.breakdowns)

    @property
    def sigma(self) -> tuple[float, ...]:
        """Approximate standard deviation of each gameweek's points.

        Two sources, both real. Rotation risk is bimodal — a coin-flip starter
        either returns or blanks entirely — and peaks at a 50% chance of playing.
        Scoring is overdispersed on top of that. This is a usable uncertainty
        band for the optimiser's risk adjustment, not a calibrated distribution.
        """
        out = []
        for minutes, breakdown in zip(self.minutes, self.breakdowns, strict=True):
            p = minutes.p_appear
            conditional = breakdown.total / p if p > 0.05 else 0.0
            rotation = p * (1 - p) * conditional**2
            scoring = p * RETURN_OVERDISPERSION * max(conditional, 0.0)
            out.append(round(math.sqrt(max(rotation + scoring, 0.0)), 3))
        return tuple(out)


def _rate(total: float, minutes: float, prior: float) -> float:
    """A per-90 rate shrunk toward ``prior`` in proportion to minutes played."""
    observed = (total / minutes * 90.0) if minutes > 0 else prior
    return shrink(observed, prior, minutes, PRIOR_MINUTES)


def compute_rates(element: Element, records: list[PlayerGameweek]) -> PlayerRates:
    """Shrunk per-90 rates from a player's completed gameweeks."""
    position = element.element_type
    minutes = float(sum(r.minutes for r in records))

    # Defensive contribution is a *threshold crossing*, so it is estimated as a
    # frequency among full appearances rather than as a per-90 rate. A player
    # averaging 9.5 actions is worth far less than one averaging 10.5, and a
    # rate-based estimate cannot see that difference at all.
    threshold = DC_THRESHOLD[position]
    if threshold is None:
        p_dc = 0.0
    else:
        starts = [r for r in records if r.played_sixty]
        crossed = sum(1 for r in starts if r.defensive_actions >= threshold)
        p_dc = shrink(
            crossed / len(starts) if starts else PRIOR_P_DC[position],
            PRIOR_P_DC[position],
            len(starts),
            PRIOR_DC_MATCHES,
        )

    return PlayerRates(
        xg90=_rate(sum(r.xg for r in records), minutes, PRIOR_XG90[position]),
        xa90=_rate(sum(r.xa for r in records), minutes, PRIOR_XA90[position]),
        bonus90=_rate(sum(r.bonus for r in records), minutes, PRIOR_BONUS90[position]),
        saves90=(
            _rate(sum(r.saves for r in records), minutes, PRIOR_SAVES90)
            if position is Position.GKP
            else 0.0
        ),
        yellow90=_rate(sum(r.yellow_cards for r in records), minutes, PRIOR_YELLOW90),
        p_dc=p_dc,
        minutes=minutes,
    )


def forecast_fixture(
    element: Element,
    rates: PlayerRates,
    minutes: MinutesForecast,
    fixture: TeamFixture,
    league: LeagueModel,
    goal_points: dict[str, int],
    clean_sheet_points: dict[str, int],
) -> PointsBreakdown:
    """Expected points for one player in one specific fixture."""
    position = element.element_type
    share = minutes.expected_minutes / 90.0

    # Only the *opponent and venue* adjust the player's attacking rate. The
    # player's own xG90 already embeds his team's attacking quality, so scaling
    # by team strength again would double-count it.
    opponent_weakness = league.rating(fixture.opponent).defence / league.league_xg
    venue = league.home_factor if fixture.home else league.away_factor
    attack_multiplier = opponent_weakness * venue

    goals = rates.xg90 * share * attack_multiplier
    assists = rates.xa90 * share * attack_multiplier

    clean_sheet = 0.0
    conceded = 0.0
    cs_value = clean_sheet_points.get(position.short, 0)
    if cs_value:
        # A clean sheet requires sixty minutes, so it is gated on p_sixty rather
        # than on appearing at all.
        p_cs = league.clean_sheet_probability(element.team, fixture.opponent, home=fixture.home)
        clean_sheet = minutes.p_sixty * p_cs * cs_value
    if position in (Position.GKP, Position.DEF):
        conceded = minutes.p_sixty * league.expected_concede_penalty(
            element.team, fixture.opponent, home=fixture.home
        )

    saves = 0.0
    if position is Position.GKP:
        # Saves scale with how much the opponent attacks.
        opponent_threat = league.expected_goals(
            fixture.opponent, element.team, home=not fixture.home
        )
        threat_multiplier = opponent_threat / league.league_xg if league.league_xg else 1.0
        saves = rates.saves90 * share * threat_multiplier / SAVES_PER_POINT

    return PointsBreakdown(
        appearance=round(minutes.appearance_points, 3),
        goals=round(goals * goal_points.get(position.short, 0), 3),
        assists=round(assists * ASSIST_POINTS, 3),
        clean_sheet=round(clean_sheet, 3),
        goals_conceded=round(conceded, 3),
        defensive_contribution=round(rates.p_dc * minutes.p_sixty * DC_POINTS, 3),
        saves=round(saves, 3),
        bonus=round(rates.bonus90 * share, 3),
        cards=round(rates.yellow90 * share * YELLOW_POINTS, 3),
    )


def _combine(breakdowns: list[PointsBreakdown]) -> PointsBreakdown:
    """Sum fixtures within one gameweek — a double counts twice, a blank scores nothing."""
    if not breakdowns:
        return PointsBreakdown()
    if len(breakdowns) == 1:
        return breakdowns[0]
    return PointsBreakdown(
        appearance=sum(b.appearance for b in breakdowns),
        goals=sum(b.goals for b in breakdowns),
        assists=sum(b.assists for b in breakdowns),
        clean_sheet=sum(b.clean_sheet for b in breakdowns),
        goals_conceded=sum(b.goals_conceded for b in breakdowns),
        defensive_contribution=sum(b.defensive_contribution for b in breakdowns),
        saves=sum(b.saves for b in breakdowns),
        bonus=sum(b.bonus for b in breakdowns),
        cards=sum(b.cards for b in breakdowns),
    )


def forecast_players(
    bootstrap: Bootstrap,
    history: History,
    league: LeagueModel,
    schedule: dict[int, list[list[TeamFixture]]],
    *,
    horizon: int,
    elements: list[Element] | None = None,
    use_availability: bool = True,
) -> dict[int, PlayerForecast]:
    """Forecast every requested player across the horizon.

    Players absent from ``schedule`` — a team with no fixtures in the window —
    receive zeroes, which is correct: a blank gameweek scores nothing.
    """
    scoring = bootstrap.game_config.scoring
    by_player = history.by_player()
    targets = elements if elements is not None else bootstrap.elements

    forecasts: dict[int, PlayerForecast] = {}
    for element in targets:
        records = by_player.get(element.id, [])
        rates = compute_rates(element, records)
        minutes = forecast_minutes(
            element, records, history.gameweeks, use_availability=use_availability
        )
        fixtures_by_step = schedule.get(element.team, [[] for _ in range(horizon)])

        breakdowns: list[PointsBreakdown] = []
        minutes_by_step: list[MinutesForecast] = []
        for step in range(horizon):
            fixtures = fixtures_by_step[step] if step < len(fixtures_by_step) else []
            breakdowns.append(
                _combine(
                    [
                        forecast_fixture(
                            element,
                            rates,
                            minutes,
                            fixture,
                            league,
                            scoring.goals_scored,
                            scoring.clean_sheets,
                        )
                        for fixture in fixtures
                    ]
                )
            )
            # A blank gameweek means no appearance either.
            minutes_by_step.append(
                minutes
                if fixtures
                else MinutesForecast(p_appear=0.0, p_sixty=0.0, expected_minutes=0.0)
            )

        forecasts[element.id] = PlayerForecast(
            element_id=element.id,
            position=element.element_type,
            team=element.team,
            name=element.name,
            minutes=minutes_by_step,
            breakdowns=breakdowns,
            rates=rates,
        )

    return forecasts

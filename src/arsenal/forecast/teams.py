"""Team strength and fixture difficulty, computed from expected goals.

FPL publishes ``team_h_difficulty`` / ``team_a_difficulty`` (FDR) and a set of
``strength_*`` fields, and neither is usable: FDR is a coarse editorial rating
set largely pre-season, and **the strength fields are unpopulated this season** —
``strength`` is null for all 20 clubs and the attack/defence values are all zero.

So difficulty is derived here from rolling xG. Attacking and defensive difficulty
are kept **separate throughout**: a team can be a fine fixture for attackers and a
terrible one for clean sheets, and collapsing that into one number destroys
exactly the distinction that matters.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from ..fpl.schemas import Bootstrap, Fixture
from .history import History

# Matches of prior weight in the shrinkage. Early in a season a team has played
# three or four games, which says very little; this holds ratings near the league
# average until real evidence accumulates.
PRIOR_MATCHES = 4.0

# Home advantage priors, used as the shrink target for the measured effect.
# Roughly the long-run Premier League values.
HOME_ATTACK_PRIOR = 1.12
AWAY_ATTACK_PRIOR = 0.90

# Goals conceded beyond this are negligible under any realistic rate.
MAX_GOALS = 10


def poisson_pmf(lam: float, k: int) -> float:
    """P(X = k) for X ~ Poisson(lam)."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def shrink(
    observed: float, prior: float, n: float, prior_weight: float = PRIOR_MATCHES
) -> float:
    """Blend an observed rate toward a prior, with confidence growing in ``n``."""
    if n <= 0:
        return prior
    return (n * observed + prior_weight * prior) / (n + prior_weight)


@dataclass(frozen=True)
class TeamRating:
    team_id: int
    matches: int
    attack: float
    """Expected goals scored per match, shrunk toward the league average."""

    defence: float
    """Expected goals conceded per match, shrunk. Higher is worse."""


@dataclass(frozen=True)
class TeamFixture:
    gameweek: int
    opponent: int
    home: bool


@dataclass
class LeagueModel:
    """A multiplicative expected-goals model over team ratings."""

    ratings: dict[int, TeamRating]
    league_xg: float
    home_factor: float
    away_factor: float

    def rating(self, team_id: int) -> TeamRating:
        return self.ratings.get(
            team_id,
            TeamRating(
                team_id=team_id, matches=0, attack=self.league_xg, defence=self.league_xg
            ),
        )

    def expected_goals(self, team_id: int, opponent_id: int, *, home: bool) -> float:
        """Expected goals for ``team_id`` against ``opponent_id``.

        The standard multiplicative form: league average, scaled by the team's
        attacking strength relative to average, by the opponent's defensive
        weakness relative to average, and by home advantage.
        """
        if self.league_xg <= 0:
            return 0.0
        attack = self.rating(team_id).attack / self.league_xg
        weakness = self.rating(opponent_id).defence / self.league_xg
        venue = self.home_factor if home else self.away_factor
        return max(0.05, self.league_xg * attack * weakness * venue)

    def clean_sheet_probability(self, team_id: int, opponent_id: int, *, home: bool) -> float:
        """P(the opponent fails to score) — Poisson at zero."""
        conceded = self.expected_goals(opponent_id, team_id, home=not home)
        return poisson_pmf(conceded, 0)

    def expected_concede_penalty(self, team_id: int, opponent_id: int, *, home: bool) -> float:
        """Expected points lost to goals conceded, for a defender or goalkeeper.

        The rule is −1 per *two* goals conceded, so the expectation is
        ``−Σ floor(k/2)·P(k)`` — not ``−E[goals]/2``. The two differ because the
        penalty is a step function: conceding one goal costs nothing at all.
        """
        conceded = self.expected_goals(opponent_id, team_id, home=not home)
        return -sum((k // 2) * poisson_pmf(conceded, k) for k in range(MAX_GOALS + 1))


def build_league_model(history: History, bootstrap: Bootstrap) -> LeagueModel:
    """Derive team ratings from completed-gameweek expected goals.

    A team's attacking output for a gameweek is the sum of its players' xG. Its
    defensive record is ``expected_goals_conceded``, which the API replicates
    onto every player in the team — so the team value is simply the maximum
    across its players that week.
    """
    team_of = {element.id: element.team for element in bootstrap.elements}

    xg_for: dict[tuple[int, int], float] = defaultdict(float)
    xg_against: dict[tuple[int, int], float] = defaultdict(float)
    for record in history.records:
        team = team_of.get(record.element_id)
        if team is None:
            continue
        key = (team, record.gameweek)
        xg_for[key] += record.xg
        xg_against[key] = max(xg_against[key], record.xgc)

    per_team_for: dict[int, list[float]] = defaultdict(list)
    per_team_against: dict[int, list[float]] = defaultdict(list)
    for (team, _), value in xg_for.items():
        per_team_for[team].append(value)
    for (team, _), value in xg_against.items():
        per_team_against[team].append(value)

    all_for = [v for values in per_team_for.values() for v in values]
    league_xg = sum(all_for) / len(all_for) if all_for else 1.4

    ratings: dict[int, TeamRating] = {}
    for team in {t.id for t in bootstrap.teams}:
        scored = per_team_for.get(team, [])
        conceded = per_team_against.get(team, [])
        matches = len(scored)
        ratings[team] = TeamRating(
            team_id=team,
            matches=matches,
            attack=shrink(sum(scored) / matches if matches else league_xg, league_xg, matches),
            defence=shrink(
                sum(conceded) / len(conceded) if conceded else league_xg, league_xg, matches
            ),
        )

    return LeagueModel(
        ratings=ratings,
        league_xg=league_xg,
        home_factor=HOME_ATTACK_PRIOR,
        away_factor=AWAY_ATTACK_PRIOR,
    )


def upcoming_fixtures(
    fixtures: list[Fixture], *, start_gameweek: int, horizon: int
) -> dict[int, list[list[TeamFixture]]]:
    """Map each team to its fixtures per horizon step.

    The inner list is indexed by horizon position, and each entry is the list of
    fixtures that team plays that gameweek. **An empty list is a blank gameweek
    and a two-entry list is a double** — both are decisive, and both are invisible
    to a naive one-fixture-per-gameweek join, which would silently record a blank
    as a difficult fixture rather than as no fixture at all.
    """
    horizon_gameweeks = list(range(start_gameweek, start_gameweek + horizon))
    schedule: dict[int, list[list[TeamFixture]]] = defaultdict(
        lambda: [[] for _ in horizon_gameweeks]
    )

    for fixture in fixtures:
        if fixture.event is None or fixture.event not in horizon_gameweeks:
            continue
        step = horizon_gameweeks.index(fixture.event)
        schedule[fixture.team_h][step].append(
            TeamFixture(gameweek=fixture.event, opponent=fixture.team_a, home=True)
        )
        schedule[fixture.team_a][step].append(
            TeamFixture(gameweek=fixture.event, opponent=fixture.team_h, home=False)
        )

    return dict(schedule)

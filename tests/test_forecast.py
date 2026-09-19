"""Forecast tests.

The emphasis is on the parts that are easy to get subtly wrong and impossible to
notice later: the defensive-contribution threshold, the goals-conceded step
function, blank and double gameweeks, and leakage in the backtest.
"""

from __future__ import annotations

import math

import pytest

from arsenal.forecast import (
    DC_THRESHOLD,
    History,
    LeagueModel,
    PlayerGameweek,
    TeamFixture,
    TeamRating,
    compute_rates,
    forecast_minutes,
    poisson_pmf,
    shrink,
    upcoming_fixtures,
)
from arsenal.forecast.backtest import spearman
from arsenal.forecast.minutes import PRIOR_P_APPEAR, availability
from arsenal.forecast.model import forecast_fixture
from arsenal.fpl.schemas import Element, Fixture, Position

GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}


def make_element(**changes) -> Element:
    base = {
        "id": 1,
        "element_type": Position.MID,
        "team": 1,
        "now_cost": 70,
        "web_name": "Test",
    }
    return Element.model_validate(base | changes)


def make_record(gameweek: int, **changes) -> PlayerGameweek:
    base = {
        "element_id": 1,
        "gameweek": gameweek,
        "minutes": 90,
        "started": True,
        "goals": 0,
        "assists": 0,
        "clean_sheet": False,
        "goals_conceded": 0,
        "saves": 0,
        "bonus": 0,
        "bps": 20,
        "yellow_cards": 0,
        "red_cards": 0,
        "defensive_actions": 0,
        "xg": 0.0,
        "xa": 0.0,
        "xgc": 1.2,
        "total_points": 2,
    }
    return PlayerGameweek(**(base | changes))


def flat_league(attack: float = 1.4, defence: float = 1.4) -> LeagueModel:
    """A league where every team is exactly average, so fixture effects vanish."""
    return LeagueModel(
        ratings={
            t: TeamRating(team_id=t, matches=5, attack=attack, defence=defence)
            for t in range(1, 21)
        },
        league_xg=1.4,
        home_factor=1.0,
        away_factor=1.0,
    )


class TestPoisson:
    def test_pmf_sums_to_one(self) -> None:
        assert sum(poisson_pmf(1.4, k) for k in range(30)) == pytest.approx(1.0, abs=1e-9)

    def test_zero_is_exponential(self) -> None:
        assert poisson_pmf(1.4, 0) == pytest.approx(math.exp(-1.4))

    def test_degenerate_rate(self) -> None:
        assert poisson_pmf(0.0, 0) == 1.0
        assert poisson_pmf(0.0, 1) == 0.0


class TestShrinkage:
    def test_no_evidence_returns_the_prior(self) -> None:
        assert shrink(observed=5.0, prior=1.0, n=0) == 1.0

    def test_converges_to_observed_with_evidence(self) -> None:
        assert shrink(5.0, 1.0, n=1000, prior_weight=4) == pytest.approx(5.0, abs=0.02)

    def test_small_samples_stay_near_the_prior(self) -> None:
        """A hot streak from one match must not be extrapolated."""
        assert shrink(10.0, 2.0, n=1, prior_weight=4) == pytest.approx(3.6)


class TestAvailability:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [("a", 1.0), ("d", 0.5), ("i", 0.0), ("s", 0.0), ("u", 0.0), ("n", 0.0)],
    )
    def test_status_codes(self, status: str, expected: float) -> None:
        assert availability(make_element(status=status)) == expected

    def test_explicit_chance_overrides_status(self) -> None:
        element = make_element(status="d", chance_of_playing_next_round=25)
        assert availability(element) == 0.25

    def test_suspension_is_zero_despite_a_stated_chance(self) -> None:
        """A suspended player cannot play whatever the percentage says."""
        element = make_element(status="s", chance_of_playing_next_round=100)
        assert availability(element) == 0.0


class TestMinutes:
    def test_absences_count_against_the_appearance_rate(self) -> None:
        """Only recorded gameweeks are appearances; the rest are evidence of absence.

        A player with one appearance in four gameweeks is a rotation risk, and a
        model that divides by appearances rather than by gameweeks would rate him
        as perfectly nailed.
        """
        nailed = forecast_minutes(
            make_element(), [make_record(g) for g in (1, 2, 3, 4)], [1, 2, 3, 4]
        )
        fringe = forecast_minutes(make_element(), [make_record(4)], [1, 2, 3, 4])
        assert nailed.p_appear > 0.8
        assert fringe.p_appear < 0.6
        assert nailed.p_appear > fringe.p_appear

    def test_recency_outweighs_older_gameweeks(self) -> None:
        """Playing recently matters more than having played in August."""
        recent = forecast_minutes(
            make_element(), [make_record(3), make_record(4)], [1, 2, 3, 4]
        )
        stale = forecast_minutes(make_element(), [make_record(1), make_record(2)], [1, 2, 3, 4])
        assert recent.p_appear > stale.p_appear

    def test_sixty_never_exceeds_appearing(self) -> None:
        cameos = [make_record(g, minutes=20) for g in (1, 2, 3, 4)]
        forecast = forecast_minutes(make_element(), cameos, [1, 2, 3, 4])
        assert forecast.p_sixty <= forecast.p_appear
        assert forecast.p_sixty < 0.3

    def test_injury_zeroes_everything(self) -> None:
        records = [make_record(g) for g in (1, 2, 3, 4)]
        forecast = forecast_minutes(make_element(status="i"), records, [1, 2, 3, 4])
        assert forecast.p_appear == 0.0
        assert forecast.expected_minutes == 0.0

    def test_no_history_falls_back_to_a_pessimistic_prior(self) -> None:
        forecast = forecast_minutes(make_element(), [], [])
        assert forecast.p_appear == pytest.approx(PRIOR_P_APPEAR)

    def test_availability_can_be_disabled_for_backtests(self) -> None:
        """Backtests must not use today's injury news to predict a past gameweek."""
        records = [make_record(g) for g in (1, 2, 3, 4)]
        injured = make_element(status="i")
        assert forecast_minutes(injured, records, [1, 2, 3, 4]).p_appear == 0.0
        leak_free = forecast_minutes(injured, records, [1, 2, 3, 4], use_availability=False)
        assert leak_free.p_appear > 0.8

    def test_appearance_points_count_both_thresholds(self) -> None:
        records = [make_record(g) for g in (1, 2, 3, 4)]
        forecast = forecast_minutes(make_element(), records, [1, 2, 3, 4])
        # Near-certain to play 60+, so close to the full 2 points.
        assert forecast.appearance_points == pytest.approx(forecast.p_appear + forecast.p_sixty)
        assert 1.6 < forecast.appearance_points <= 2.0


class TestDefensiveContribution:
    def test_thresholds_match_the_verified_values(self) -> None:
        assert DC_THRESHOLD[Position.DEF] == 10
        assert DC_THRESHOLD[Position.MID] == 12
        assert DC_THRESHOLD[Position.FWD] == 12
        assert DC_THRESHOLD[Position.GKP] is None

    def test_is_a_threshold_not_a_rate(self) -> None:
        """Nine actions every week is worth nothing; eleven is worth two points.

        A rate-based model would rate these two players within 20% of each other.
        The real gap is the entire award.
        """
        element = make_element(element_type=Position.DEF)
        just_under = compute_rates(
            element, [make_record(g, defensive_actions=9) for g in range(1, 5)]
        )
        just_over = compute_rates(
            element, [make_record(g, defensive_actions=11) for g in range(1, 5)]
        )
        assert just_under.p_dc < 0.2
        assert just_over.p_dc > 0.6

    def test_goalkeepers_are_never_credited(self) -> None:
        element = make_element(element_type=Position.GKP)
        rates = compute_rates(
            element, [make_record(g, defensive_actions=30) for g in range(1, 5)]
        )
        assert rates.p_dc == 0.0

    def test_forwards_carry_a_near_zero_prior(self) -> None:
        """No forward crossed the threshold in 140 player-gameweeks of real data."""
        element = make_element(element_type=Position.FWD)
        rates = compute_rates(element, [])
        assert rates.p_dc < 0.05


class TestGoalsConcededPenalty:
    def test_is_a_step_function_not_a_linear_rate(self) -> None:
        """Conceding one goal costs nothing, so -E[goals]/2 overstates the penalty."""
        league = flat_league()
        penalty = league.expected_concede_penalty(1, 2, home=True)
        naive = -league.expected_goals(2, 1, home=False) / 2
        assert penalty > naive  # less negative
        assert penalty < 0

    def test_worse_opponents_cost_more(self) -> None:
        league = flat_league()
        league.ratings[5] = TeamRating(team_id=5, matches=5, attack=3.0, defence=1.4)
        assert league.expected_concede_penalty(
            1, 5, home=True
        ) < league.expected_concede_penalty(1, 2, home=True)


class TestCleanSheets:
    def test_probability_falls_as_the_opponent_improves(self) -> None:
        league = flat_league()
        league.ratings[5] = TeamRating(team_id=5, matches=5, attack=3.0, defence=1.4)
        assert league.clean_sheet_probability(1, 5, home=True) < league.clean_sheet_probability(
            1, 2, home=True
        )

    def test_requires_sixty_minutes(self) -> None:
        """A defender unlikely to last an hour cannot be paid for a clean sheet."""
        element = make_element(element_type=Position.DEF)
        rates = compute_rates(element, [make_record(g) for g in range(1, 5)])
        league = flat_league()
        fixture = TeamFixture(gameweek=6, opponent=2, home=True)

        from arsenal.forecast.minutes import MinutesForecast

        nailed = MinutesForecast(p_appear=1.0, p_sixty=1.0, expected_minutes=90)
        cameo = MinutesForecast(p_appear=1.0, p_sixty=0.0, expected_minutes=30)
        full = forecast_fixture(
            element, rates, nailed, fixture, league, GOAL_POINTS, CLEAN_SHEET_POINTS
        )
        partial = forecast_fixture(
            element, rates, cameo, fixture, league, GOAL_POINTS, CLEAN_SHEET_POINTS
        )
        assert full.clean_sheet > 0
        assert partial.clean_sheet == 0.0

    def test_forwards_get_nothing_for_clean_sheets(self) -> None:
        element = make_element(element_type=Position.FWD)
        rates = compute_rates(element, [make_record(g) for g in range(1, 5)])
        from arsenal.forecast.minutes import MinutesForecast

        breakdown = forecast_fixture(
            element,
            rates,
            MinutesForecast(1.0, 1.0, 90),
            TeamFixture(gameweek=6, opponent=2, home=True),
            flat_league(),
            GOAL_POINTS,
            CLEAN_SHEET_POINTS,
        )
        assert breakdown.clean_sheet == 0.0


class TestFixtures:
    def fixtures(self) -> list[Fixture]:
        return [
            Fixture.model_validate({"id": 1, "event": 6, "team_h": 1, "team_a": 2}),
            Fixture.model_validate({"id": 2, "event": 7, "team_h": 3, "team_a": 1}),
            # Team 1 plays twice in GW8 — a double gameweek.
            Fixture.model_validate({"id": 3, "event": 8, "team_h": 1, "team_a": 4}),
            Fixture.model_validate({"id": 4, "event": 8, "team_h": 5, "team_a": 1}),
            Fixture.model_validate({"id": 5, "event": 6, "team_h": 3, "team_a": 4}),
        ]

    def test_detects_a_double_gameweek(self) -> None:
        schedule = upcoming_fixtures(self.fixtures(), start_gameweek=6, horizon=3)
        assert len(schedule[1][2]) == 2

    def test_detects_a_blank_gameweek(self) -> None:
        """Team 2 has no GW7 fixture. That is no fixture, not a hard fixture."""
        schedule = upcoming_fixtures(self.fixtures(), start_gameweek=6, horizon=3)
        assert schedule[2][1] == []

    def test_home_and_away_are_recorded(self) -> None:
        schedule = upcoming_fixtures(self.fixtures(), start_gameweek=6, horizon=3)
        assert schedule[1][0][0].home is True
        assert schedule[1][1][0].home is False

    def test_unscheduled_fixtures_are_skipped(self) -> None:
        """Late-season fixtures often have a null event and must not be invented."""
        unscheduled = [
            Fixture.model_validate({"id": 9, "event": None, "team_h": 1, "team_a": 2})
        ]
        assert upcoming_fixtures(unscheduled, start_gameweek=6, horizon=3) == {}


class TestSpearman:
    def test_perfect_agreement(self) -> None:
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_disagreement(self) -> None:
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_handles_ties_without_blowing_up(self) -> None:
        assert spearman([1, 1, 1, 1], [1, 2, 3, 4]) == 0.0


class TestHistory:
    def test_indexes_and_sorts_by_player(self) -> None:
        history = History(
            records=[
                make_record(3),
                make_record(1),
                make_record(2, **{"element_id": 2}),
            ],
            gameweeks=[1, 2, 3],
        )
        by_player = history.by_player()
        assert [r.gameweek for r in by_player[1]] == [1, 3]
        assert len(by_player[2]) == 1

    def test_earned_defensive_contribution(self) -> None:
        assert make_record(1, defensive_actions=10).earned_defensive_contribution(Position.DEF)
        assert not make_record(1, defensive_actions=9).earned_defensive_contribution(
            Position.DEF
        )
        assert not make_record(1, defensive_actions=10).earned_defensive_contribution(
            Position.MID
        )
        assert make_record(1, defensive_actions=12).earned_defensive_contribution(Position.MID)

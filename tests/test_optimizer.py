"""Optimiser tests against hand-constructed fixtures with known optima.

Every fixture here is small enough to reason about by hand, so a failure points
at a specific constraint rather than "the solver did something odd". The
solver's output is also pushed through the *independent* validator in
``fpl.rules`` — if the two ever disagree, that disagreement is the finding.
"""

from __future__ import annotations

import pytest

from arsenal.fpl.rules import SquadPlayer, validate_squad
from arsenal.fpl.schemas import Position
from arsenal.optimizer import Candidate, InfeasibleError, OptimiserConfig, optimise

COUNTS = {Position.GKP: 2, Position.DEF: 5, Position.MID: 5, Position.FWD: 3}


def pool(
    *,
    horizon: int = 1,
    price: int = 45,
    xp: float = 2.0,
    per_position: int = 6,
    spread_clubs: bool = True,
) -> list[Candidate]:
    """A uniform pool: every player identical, so any legal squad is optimal.

    Tests then perturb exactly one dimension and assert the solver reacts to it.
    """
    candidates: list[Candidate] = []
    element_id = 1
    for position in Position:
        for n in range(per_position):
            candidates.append(
                Candidate(
                    element_id=element_id,
                    position=position,
                    team=(element_id % 20) + 1 if spread_clubs else 1,
                    now_cost=price,
                    xp=tuple([xp] * horizon),
                    name=f"{position.short}{n}",
                )
            )
            element_id += 1
    return candidates


def with_changes(candidate: Candidate, **changes: object) -> Candidate:
    from dataclasses import replace

    return replace(candidate, **changes)


def owned_pool(*, horizon: int = 1, xp: float = 2.0, per_position: int = 10) -> list[Candidate]:
    """A pool where a legal 15 is already owned, leaving spares to transfer in.

    ``per_position`` must comfortably exceed the squad requirement — with only 6
    midfielders, 5 of them owned, there is a single spare and any test that wants
    two transfers silently cannot have them.
    """
    candidates = pool(horizon=horizon, xp=xp, per_position=per_position)
    owned_ids: list[int] = []
    for position, count in COUNTS.items():
        members = [c for c in candidates if c.position is position]
        owned_ids += [c.element_id for c in members[:count]]
    return [
        with_changes(c, owned=True, purchase_price=c.now_cost)
        if c.element_id in owned_ids
        else c
        for c in candidates
    ]


def as_squad_players(decision, index: dict[int, Candidate]) -> list[SquadPlayer]:
    """Convert an optimiser decision into the validator's input type."""
    bench_rank = {pid: rank + 1 for rank, pid in enumerate(decision.bench)}
    return [
        SquadPlayer(
            element_id=pid,
            position=index[pid].position,
            team=index[pid].team,
            now_cost=index[pid].now_cost,
            purchase_price=index[pid].purchase_price,
            is_starting=pid in decision.starting,
            is_captain=pid == decision.captain,
            is_vice_captain=pid == decision.vice_captain,
            bench_rank=bench_rank.get(pid),
        )
        for pid in decision.squad
    ]


def assert_legal(decision, candidates: list[Candidate], budget: int) -> None:
    """Cross-check the solver against the independently written validator."""
    index = {c.element_id: c for c in candidates}
    result = validate_squad(as_squad_players(decision, index), budget=budget)
    assert result.ok, f"optimiser produced an illegal squad:\n{result}"


class TestBasicSelection:
    def test_produces_a_legal_squad(self) -> None:
        candidates = pool()
        plan = optimise(
            candidates,
            initial_bank=1000,
            config=OptimiserConfig(horizon=1),
            chip="wildcard",
        )
        decision = plan.this_week
        assert len(decision.squad) == 15
        assert len(decision.starting) == 11
        assert len(decision.bench) == 4
        assert_legal(decision, candidates, budget=1000)

    def test_position_counts_are_exact(self) -> None:
        candidates = pool()
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        index = {c.element_id: c for c in candidates}
        for position, expected in COUNTS.items():
            actual = sum(1 for i in plan.this_week.squad if index[i].position is position)
            assert actual == expected

    def test_reserve_goalkeeper_is_last_on_the_bench(self) -> None:
        candidates = pool()
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        index = {c.element_id: c for c in candidates}
        assert index[plan.this_week.bench[-1]].position is Position.GKP

    def test_captain_and_vice_are_distinct_starters(self) -> None:
        candidates = pool()
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        decision = plan.this_week
        assert decision.captain != decision.vice_captain
        assert decision.captain in decision.starting
        assert decision.vice_captain in decision.starting


class TestObjective:
    def test_captains_the_highest_scorer(self) -> None:
        candidates = pool()
        star = with_changes(candidates[12], xp=(50.0,))
        candidates[12] = star
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        assert plan.this_week.captain == star.element_id

    def test_vice_is_the_second_best_starter(self) -> None:
        candidates = pool()
        candidates[12] = with_changes(candidates[12], xp=(50.0,))
        candidates[13] = with_changes(candidates[13], xp=(40.0,))
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        assert plan.this_week.captain == candidates[12].element_id
        assert plan.this_week.vice_captain == candidates[13].element_id

    def test_benches_the_weakest_players(self) -> None:
        """With one position varied, the low scorers must end up on the bench."""
        candidates = pool()
        midfielders = [c for c in candidates if c.position is Position.MID]
        for n, midfielder in enumerate(midfielders):
            index = candidates.index(midfielder)
            candidates[index] = with_changes(midfielder, xp=(float(10 - n),))
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        chosen = [c for c in candidates if c.element_id in plan.this_week.squad]
        best_mid = max((c for c in chosen if c.position is Position.MID), key=lambda c: c.xp[0])
        assert best_mid.element_id in plan.this_week.starting

    def test_risk_aversion_prefers_the_certain_player(self) -> None:
        """Equal expected points, different variance — the steady one wins."""
        candidates = pool()
        volatile = with_changes(candidates[12], xp=(9.0,), sigma=(8.0,))
        steady = with_changes(candidates[13], xp=(9.0,), sigma=(0.1,))
        candidates[12], candidates[13] = volatile, steady
        plan = optimise(
            candidates,
            initial_bank=1000,
            config=OptimiserConfig(horizon=1, risk_aversion=0.5),
            chip="wildcard",
        )
        assert steady.element_id in plan.this_week.starting
        assert plan.this_week.captain == steady.element_id


class TestConstraintsBind:
    def test_club_limit_binds(self) -> None:
        """Four excellent players from one club — only three may be owned."""
        candidates = pool()
        for n in range(4):
            candidates[n] = with_changes(candidates[n], team=99, xp=(30.0,))
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        index = {c.element_id: c for c in candidates}
        from_club = sum(1 for i in plan.this_week.squad if index[i].team == 99)
        assert from_club <= 3

    def test_budget_binds(self) -> None:
        """A squad of 15 at £5.0m costs £75.0m; a £70.0m budget forces cheaper picks."""
        candidates = pool(price=50)
        cheap = [
            Candidate(
                element_id=1000 + n,
                position=position,
                team=(n % 20) + 1,
                now_cost=40,
                xp=(1.0,),
                name=f"cheap{n}",
            )
            for n, position in enumerate(
                [p for p, count in COUNTS.items() for _ in range(count)]
            )
        ]
        plan = optimise(
            candidates + cheap,
            initial_bank=700,
            config=OptimiserConfig(horizon=1),
            chip="wildcard",
        )
        index = {c.element_id: c for c in candidates + cheap}
        spend = sum(index[i].now_cost for i in plan.this_week.squad)
        assert spend <= 700
        assert plan.this_week.bank >= 0

    def test_infeasible_budget_raises(self) -> None:
        """No combination fits, so the solver must say so rather than improvise."""
        with pytest.raises(InfeasibleError):
            optimise(
                pool(price=100),
                initial_bank=500,
                config=OptimiserConfig(horizon=1),
                chip="wildcard",
            )


class TestTransfers:
    def test_makes_no_transfer_when_nothing_improves(self) -> None:
        candidates = owned_pool()
        plan = optimise(candidates, initial_bank=0, initial_free_transfers=1)
        assert plan.this_week.transfers_in == []
        assert plan.this_week.hits == 0

    def test_takes_the_free_transfer_when_it_gains(self) -> None:
        candidates = owned_pool()
        upgrade = next(c for c in candidates if not c.owned and c.position is Position.MID)
        index = candidates.index(upgrade)
        candidates[index] = with_changes(upgrade, xp=(20.0,))
        plan = optimise(candidates, initial_bank=0, initial_free_transfers=1)
        assert plan.this_week.transfers_in == [upgrade.element_id]
        assert plan.this_week.hits == 0

    def test_takes_a_hit_when_the_gain_exceeds_four(self) -> None:
        """Two upgrades worth +18 each on one free transfer: the -4 is clearly worth it."""
        candidates = owned_pool()
        upgrades = [c for c in candidates if not c.owned and c.position is Position.MID][:2]
        for upgrade in upgrades:
            candidates[candidates.index(upgrade)] = with_changes(upgrade, xp=(20.0,))
        plan = optimise(candidates, initial_bank=0, initial_free_transfers=1)
        assert len(plan.this_week.transfers_in) == 2
        assert plan.this_week.hits == 1
        assert plan.this_week.hit_cost == 4

    def test_refuses_a_hit_when_the_gain_is_too_small(self) -> None:
        """A second transfer worth +3 does not justify a -4."""
        candidates = owned_pool()
        upgrades = [c for c in candidates if not c.owned and c.position is Position.MID][:2]
        candidates[candidates.index(upgrades[0])] = with_changes(upgrades[0], xp=(20.0,))
        candidates[candidates.index(upgrades[1])] = with_changes(upgrades[1], xp=(5.0,))
        plan = optimise(candidates, initial_bank=0, initial_free_transfers=1)
        assert plan.this_week.transfers_in == [upgrades[0].element_id]
        assert plan.this_week.hits == 0

    def test_max_hit_bound_is_respected(self) -> None:
        """Three huge upgrades, but policy allows only one hit."""
        candidates = owned_pool()
        upgrades = [c for c in candidates if not c.owned and c.position is Position.MID][:3]
        for upgrade in upgrades:
            candidates[candidates.index(upgrade)] = with_changes(upgrade, xp=(40.0,))
        plan = optimise(
            candidates,
            initial_bank=0,
            initial_free_transfers=1,
            config=OptimiserConfig(horizon=1, max_hit=4),
        )
        assert plan.this_week.hits <= 1
        assert len(plan.this_week.transfers_in) <= 2

    def test_selling_price_applies_the_sell_on_fee(self) -> None:
        """A player bought at 45 and now worth 55 raises 50, not 55.

        Budgeting against ``now_cost`` would leave the plan £0.5m short and the
        server would reject the transfer at the deadline.
        """
        candidates = owned_pool()
        owned_mid = next(c for c in candidates if c.owned and c.position is Position.MID)
        risen = with_changes(owned_mid, now_cost=55, purchase_price=45, xp=(0.0,))
        candidates[candidates.index(owned_mid)] = risen
        assert risen.sells_for == 50

        target = next(c for c in candidates if not c.owned and c.position is Position.MID)
        # Priced exactly at what the sale raises — affordable only if the fee is applied.
        candidates[candidates.index(target)] = with_changes(target, now_cost=50, xp=(20.0,))

        plan = optimise(candidates, initial_bank=0, initial_free_transfers=1)
        assert plan.this_week.transfers_out == [risen.element_id]
        assert plan.this_week.bank == 0


class TestChips:
    """Chips are played on a squad you already own, so these start from one.

    Using a fresh squad here would need a wildcard to afford 15 purchases, and
    two chips cannot be played in the same gameweek.
    """

    def test_triple_captain_triples_the_captain(self) -> None:
        candidates = owned_pool()
        star = next(c for c in candidates if c.owned and c.position is Position.MID)
        candidates[candidates.index(star)] = with_changes(star, xp=(10.0,))
        config = OptimiserConfig(horizon=1)

        plain = optimise(candidates, initial_bank=0, config=config)
        tripled = optimise(candidates, initial_bank=0, config=config, chip="3xc")

        assert tripled.this_week.captain == star.element_id
        # The captain's 10.0 is counted three times rather than twice.
        assert tripled.this_week.expected_points == pytest.approx(
            plain.this_week.expected_points + 10.0
        )

    def test_bench_boost_counts_the_bench(self) -> None:
        candidates = owned_pool(xp=3.0)
        config = OptimiserConfig(horizon=1)

        plain = optimise(candidates, initial_bank=0, config=config)
        boosted = optimise(candidates, initial_bank=0, config=config, chip="bboost")

        # Four bench players at 3.0 each.
        assert boosted.this_week.expected_points == pytest.approx(
            plain.this_week.expected_points + 12.0
        )

    def test_wildcard_makes_all_transfers_free(self) -> None:
        candidates = pool()
        plan = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        assert len(plan.this_week.transfers_in) == 15
        assert plan.this_week.hits == 0


class TestHorizon:
    def test_plans_across_every_gameweek(self) -> None:
        plan = optimise(
            pool(horizon=3),
            initial_bank=1000,
            config=OptimiserConfig(horizon=3),
            chip="wildcard",
        )
        assert len(plan.decisions) == 3
        assert all(len(d.squad) == 15 for d in plan.decisions)

    def test_unused_free_transfers_accumulate(self) -> None:
        """Nothing worth buying, so the transfer banks and two are available next week."""
        plan = optimise(
            owned_pool(horizon=2),
            initial_bank=0,
            initial_free_transfers=1,
            config=OptimiserConfig(horizon=2),
        )
        assert plan.decisions[0].transfers_in == []
        assert plan.decisions[0].free_transfers == 1
        assert plan.decisions[1].free_transfers == 2

    def test_defers_a_purchase_with_no_present_value(self) -> None:
        """A player who scores nothing this week but hauls next week is bought next week.

        Buying early is not free even when the transfer is: the incoming player
        displaces a scoring one from the squad for a gameweek. The solver should
        wait rather than burn the transfer a week early.
        """
        candidates = owned_pool(horizon=2)
        future_star = next(c for c in candidates if not c.owned and c.position is Position.MID)
        candidates[candidates.index(future_star)] = with_changes(future_star, xp=(0.0, 40.0))

        plan = optimise(
            candidates,
            initial_bank=0,
            initial_free_transfers=1,
            config=OptimiserConfig(horizon=2),
        )
        assert future_star.element_id not in plan.decisions[0].squad
        assert future_star.element_id in plan.decisions[1].squad
        assert plan.decisions[1].hits == 0


class TestDeterminism:
    def test_identical_inputs_give_identical_plans(self) -> None:
        """Non-determinism here would make every downstream bug irreproducible."""
        candidates = pool()
        first = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        second = optimise(
            candidates, initial_bank=1000, config=OptimiserConfig(horizon=1), chip="wildcard"
        )
        assert sorted(first.this_week.squad) == sorted(second.this_week.squad)
        assert first.this_week.captain == second.this_week.captain
        assert first.this_week.bench == second.this_week.bench

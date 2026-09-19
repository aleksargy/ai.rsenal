"""Squad validation.

Every test here corresponds to an invariant in the ``fpl-rules`` skill. This
validator is the last gate before an irreversible submission, so each rule is
tested in isolation — a validator that catches four of five violations is a
validator you cannot trust.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from arsenal.fpl.rules import (
    DEFAULT_PLAY_LIMITS,
    SquadPlayer,
    bank_free_transfers,
    transfer_cost,
    valid_formations,
    validate_squad,
)
from arsenal.fpl.schemas import Position


def make_squad(
    *,
    formation: tuple[int, int, int] = (4, 4, 2),
    clubs: list[int] | None = None,
    price: int = 50,
) -> list[SquadPlayer]:
    """Build a legal 15 with the given starting formation.

    Order is stable and positional — goalkeepers first, then defenders,
    midfielders, forwards — so a test can reach for ``squad[0]`` and know it has
    a starting goalkeeper. Clubs are spread so the 3-per-club limit passes unless
    a test deliberately overrides it.
    """
    n_def, n_mid, n_fwd = formation
    counts = {Position.GKP: 2, Position.DEF: 5, Position.MID: 5, Position.FWD: 3}
    starters = {Position.GKP: 1, Position.DEF: n_def, Position.MID: n_mid, Position.FWD: n_fwd}

    squad: list[SquadPlayer] = []
    for position, total in counts.items():
        for index in range(total):
            club = clubs[len(squad)] if clubs else (len(squad) % 20) + 1
            squad.append(
                SquadPlayer(
                    element_id=len(squad) + 1,
                    position=position,
                    team=club,
                    now_cost=price,
                    purchase_price=price,
                    is_starting=index < starters[position],
                )
            )

    # Bench: outfield substitutes ranked 1-3, reserve goalkeeper always last.
    outfield_rank = 0
    ranked: list[SquadPlayer] = []
    for player in squad:
        if player.is_starting:
            ranked.append(player)
        elif player.position is Position.GKP:
            ranked.append(replace(player, bench_rank=4))
        else:
            outfield_rank += 1
            ranked.append(replace(player, bench_rank=outfield_rank))

    # Captain and vice are the first two starters.
    starter_indices = [i for i, p in enumerate(ranked) if p.is_starting]
    ranked[starter_indices[0]] = replace(ranked[starter_indices[0]], is_captain=True)
    ranked[starter_indices[1]] = replace(ranked[starter_indices[1]], is_vice_captain=True)
    return ranked


class TestValidSquad:
    def test_baseline_squad_passes(self) -> None:
        result = validate_squad(make_squad(), budget=1000)
        assert result.ok, str(result)

    @pytest.mark.parametrize("formation", sorted(valid_formations()))
    def test_every_legal_formation_passes(self, formation: tuple[int, int, int]) -> None:
        result = validate_squad(make_squad(formation=formation), budget=1000)
        assert result.ok, f"{formation}: {result}"


class TestFormations:
    def test_derived_set_matches_the_known_rules(self) -> None:
        """Legal formations are derived from play limits, not hardcoded."""
        assert valid_formations() == {
            (3, 4, 3),
            (3, 5, 2),
            (4, 3, 3),
            (4, 4, 2),
            (4, 5, 1),
            (5, 2, 3),
            (5, 3, 2),
            (5, 4, 1),
        }

    def test_every_formation_fields_ten_outfield(self) -> None:
        assert all(sum(f) == 10 for f in valid_formations())

    def test_respects_custom_limits(self) -> None:
        """A rule change to the play limits must flow through automatically."""
        limits = {**DEFAULT_PLAY_LIMITS, Position.FWD: (0, 3)}
        assert (5, 5, 0) in valid_formations(limits)
        assert (5, 5, 0) not in valid_formations()


class TestSquadShape:
    def test_rejects_wrong_size(self) -> None:
        result = validate_squad(make_squad()[:14], budget=1000)
        assert not result.ok
        assert any("14 players" in e for e in result.errors)

    def test_rejects_wrong_position_counts(self) -> None:
        squad = make_squad()
        squad[0] = replace(squad[0], position=Position.MID)
        result = validate_squad(squad, budget=1000)
        assert not result.ok
        assert any("GKP" in e for e in result.errors)

    def test_rejects_duplicate_players(self) -> None:
        squad = make_squad()
        squad[1] = replace(squad[1], element_id=squad[0].element_id)
        result = validate_squad(squad, budget=1000)
        assert any("duplicate" in e for e in result.errors)

    def test_rejects_illegal_formation(self) -> None:
        squad = make_squad(formation=(4, 4, 2))
        forwards = [p for p in squad if p.position is Position.FWD]
        squad = [p for p in squad if p.position is not Position.FWD]
        squad += [replace(f, is_starting=True, bench_rank=None) for f in forwards]
        result = validate_squad(squad, budget=1000)
        assert not result.ok


class TestClubLimit:
    def test_rejects_four_from_one_club(self) -> None:
        clubs = [7, 7, 7, 7] + [(i % 19) + 8 for i in range(11)]
        result = validate_squad(make_squad(clubs=clubs), budget=1000)
        assert not result.ok
        assert any("team 7" in e for e in result.errors)

    def test_allows_exactly_three(self) -> None:
        clubs = [7, 7, 7] + [(i % 19) + 8 for i in range(12)]
        result = validate_squad(make_squad(clubs=clubs), budget=1000)
        assert result.ok, str(result)


class TestBudget:
    def test_rejects_overspend(self) -> None:
        result = validate_squad(make_squad(price=70), budget=1000)
        assert not result.ok
        assert any("over by" in e for e in result.errors)

    def test_budget_uses_selling_price_not_current_price(self) -> None:
        """The sell-on fee means squad value overstates real spending power.

        Fifteen players bought at 50 and now worth 60 sell for 55 each: 825 total,
        not the 900 their current prices suggest. A validator that used
        ``now_cost`` would wave through a squad the server rejects.
        """
        squad = [replace(p, now_cost=60, purchase_price=50) for p in make_squad()]
        assert sum(p.sells_for for p in squad) == 825
        assert validate_squad(squad, budget=825).ok
        assert not validate_squad(squad, budget=824).ok

    def test_unowned_players_sell_at_current_price(self) -> None:
        player = SquadPlayer(
            element_id=1, position=Position.MID, team=1, now_cost=95, purchase_price=None
        )
        assert player.sells_for == 95


class TestCaptaincy:
    def test_rejects_missing_captain(self) -> None:
        squad = [replace(p, is_captain=False) for p in make_squad()]
        result = validate_squad(squad, budget=1000)
        assert any("0 captains" in e for e in result.errors)

    def test_rejects_two_captains(self) -> None:
        squad = make_squad()
        squad = [
            replace(p, is_captain=True) if p.is_starting and not p.is_vice_captain else p
            for p in squad
        ]
        result = validate_squad(squad, budget=1000)
        assert any("captains, expected exactly 1" in e for e in result.errors)

    def test_rejects_benched_captain(self) -> None:
        squad = make_squad()
        squad = [replace(p, is_captain=False) for p in squad]
        bench = next(p for p in squad if not p.is_starting)
        squad = [replace(p, is_captain=True) if p is bench else p for p in squad]
        result = validate_squad(squad, budget=1000)
        assert any("captain is not in the starting XI" in e for e in result.errors)

    def test_rejects_same_captain_and_vice(self) -> None:
        squad = make_squad()
        squad = [replace(p, is_vice_captain=False) for p in squad]
        captain = next(p for p in squad if p.is_captain)
        squad = [replace(p, is_vice_captain=True) if p is captain else p for p in squad]
        result = validate_squad(squad, budget=1000)
        assert any("same player" in e for e in result.errors)


class TestBenchOrder:
    def test_rejects_reserve_gk_out_of_last_slot(self) -> None:
        squad = make_squad()
        bench = [p for p in squad if not p.is_starting]
        gk = next(p for p in bench if p.position is Position.GKP)
        other = next(p for p in bench if p.position is not Position.GKP and p.bench_rank == 1)
        squad = [
            replace(p, bench_rank=1)
            if p is gk
            else replace(p, bench_rank=4)
            if p is other
            else p
            for p in squad
        ]
        result = validate_squad(squad, budget=1000)
        assert any("reserve goalkeeper" in e for e in result.errors)

    def test_rejects_duplicate_bench_ranks(self) -> None:
        squad = make_squad()
        bench = [p for p in squad if not p.is_starting]
        squad = [replace(p, bench_rank=1) if p in bench[:2] else p for p in squad]
        result = validate_squad(squad, budget=1000)
        assert any("bench ranks" in e for e in result.errors)


class TestTransferCost:
    @pytest.mark.parametrize(
        ("transfers", "free", "expected"),
        [(0, 1, 0), (1, 1, 0), (2, 1, 4), (3, 1, 8), (5, 5, 0), (6, 5, 4), (2, 0, 8)],
    )
    def test_hits(self, transfers: int, free: int, expected: int) -> None:
        assert transfer_cost(transfers, free) == expected

    @pytest.mark.parametrize("chip", ["wildcard", "freehit"])
    def test_chips_make_transfers_free(self, chip: str) -> None:
        assert transfer_cost(15, 1, chip=chip) == 0


class TestFreeTransferBanking:
    @pytest.mark.parametrize(
        ("current", "used", "expected"),
        [
            (1, 0, 2),  # unused transfer banks
            (1, 1, 1),  # used it, back to one
            (5, 0, 5),  # already at the cap, stays there
            (5, 1, 5),  # used one of five, refills to the cap
            (4, 0, 5),  # reaches the cap
            (2, 3, 1),  # took a hit; never goes negative
        ],
    )
    def test_banking(self, current: int, used: int, expected: int) -> None:
        assert bank_free_transfers(current, used) == expected

    def test_never_exceeds_cap(self) -> None:
        assert all(bank_free_transfers(n, 0) <= 5 for n in range(0, 10))

    @pytest.mark.parametrize("chip", ["wildcard", "freehit"])
    def test_chip_resets_to_one(self, chip: str) -> None:
        """A chip does not bank transfers — you return to 1 the following week."""
        assert bank_free_transfers(5, 15, chip=chip) == 1

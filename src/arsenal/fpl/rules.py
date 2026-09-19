"""Rules engine and squad validator.

This module is the **independent check** on the optimiser. It deliberately shares
no constraint code with ``arsenal.optimizer``: a bug duplicated in both the
solver and its validator is invisible precisely when it matters, which is at the
deadline, on a submission that cannot be undone.

So this is written from the rulebook (see the ``fpl-rules`` skill), not from the
LP formulation. If the two ever disagree, that disagreement is the finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..money import format_money, selling_price
from .schemas import Availability, Bootstrap, Element, Position

# Squad shape. Defaults match the current season, but callers should pass the
# live values from `bootstrap.squad_requirements()` so a rule change is picked up
# automatically rather than requiring a code edit.
DEFAULT_SQUAD_REQUIREMENTS: dict[Position, int] = {
    Position.GKP: 2,
    Position.DEF: 5,
    Position.MID: 5,
    Position.FWD: 3,
}

DEFAULT_PLAY_LIMITS: dict[Position, tuple[int, int]] = {
    Position.GKP: (1, 1),
    Position.DEF: (3, 5),
    Position.MID: (2, 5),
    Position.FWD: (1, 3),
}

SQUAD_SIZE = 15
STARTING_XI = 11
MAX_PER_CLUB = 3
HIT_COST = 4


@dataclass(frozen=True)
class SquadPlayer:
    """A player in a proposed squad, with everything needed to validate them."""

    element_id: int
    position: Position
    team: int
    now_cost: int  # tenths
    purchase_price: int | None = None  # None for a player not currently owned
    is_starting: bool = False
    is_captain: bool = False
    is_vice_captain: bool = False
    bench_rank: int | None = None  # 1-4 among bench; 4 is the reserve GK slot

    @property
    def sells_for(self) -> int:
        """Selling value in tenths. Equals ``now_cost`` for an unowned player."""
        if self.purchase_price is None:
            return self.now_cost
        return selling_price(self.purchase_price, self.now_cost)


@dataclass
class ValidationResult:
    """Outcome of validating a proposed squad.

    ``errors`` block submission unconditionally. ``warnings`` are surfaced in the
    notification but do not abort — a doubtful player is legal, just risky.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def __str__(self) -> str:
        lines = [f"ERROR: {e}" for e in self.errors]
        lines += [f"WARN:  {w}" for w in self.warnings]
        return "\n".join(lines) or "OK"


def valid_formations(
    play_limits: dict[Position, tuple[int, int]] | None = None,
    starting_xi: int = STARTING_XI,
) -> set[tuple[int, int, int]]:
    """Derive legal (DEF, MID, FWD) formations from the positional play limits.

    Derived rather than hardcoded so a rule change to ``squad_min_play`` /
    ``squad_max_play`` is picked up from the API automatically.
    """
    limits = play_limits or DEFAULT_PLAY_LIMITS
    gk_min, _ = limits[Position.GKP]
    d_lo, d_hi = limits[Position.DEF]
    m_lo, m_hi = limits[Position.MID]
    f_lo, f_hi = limits[Position.FWD]
    outfield = starting_xi - gk_min
    return {
        (d, m, f)
        for d in range(d_lo, d_hi + 1)
        for m in range(m_lo, m_hi + 1)
        for f in range(f_lo, f_hi + 1)
        if d + m + f == outfield
    }


def transfer_cost(n_transfers: int, free_transfers: int, *, chip: str | None = None) -> int:
    """Points cost of making ``n_transfers``.

    Wildcard and Free Hit make every transfer free.
    """
    if chip in ("wildcard", "freehit"):
        return 0
    return max(0, n_transfers - free_transfers) * HIT_COST


def bank_free_transfers(
    current_free: int, used: int, *, max_free: int = 5, chip: str | None = None
) -> int:
    """Free transfers carried into the next gameweek.

    A Wildcard or Free Hit does not bank: you return to 1 FT regardless of how
    many transfers the chip allowed.
    """
    if chip in ("wildcard", "freehit"):
        return 1
    return min(max_free, max(0, current_free - used) + 1)


def validate_squad(
    squad: list[SquadPlayer],
    *,
    bank: int = 0,
    budget: int | None = None,
    squad_requirements: dict[Position, int] | None = None,
    play_limits: dict[Position, tuple[int, int]] | None = None,
    elements: dict[int, Element] | None = None,
    chip: str | None = None,
) -> ValidationResult:
    """Check every invariant from the ``fpl-rules`` skill.

    ``budget`` is the total spend allowed in tenths. When omitted it is taken as
    the squad's own selling value plus ``bank``, which checks internal
    consistency but cannot catch an overspend — so pass it explicitly before any
    real submission.
    """
    requirements = squad_requirements or DEFAULT_SQUAD_REQUIREMENTS
    limits = play_limits or DEFAULT_PLAY_LIMITS
    result = ValidationResult()

    # --- 1. Squad size and shape -------------------------------------------
    if len(squad) != SQUAD_SIZE:
        result.error(f"squad has {len(squad)} players, expected {SQUAD_SIZE}")

    ids = [p.element_id for p in squad]
    if len(set(ids)) != len(ids):
        duplicates = {i for i in ids if ids.count(i) > 1}
        result.error(f"duplicate players in squad: {sorted(duplicates)}")

    for position, expected in requirements.items():
        actual = sum(1 for p in squad if p.position is position)
        if actual != expected:
            result.error(f"{position.short}: {actual} players, expected {expected}")

    # --- 2. Club limit ------------------------------------------------------
    by_club: dict[int, int] = {}
    for p in squad:
        by_club[p.team] = by_club.get(p.team, 0) + 1
    for club, count in sorted(by_club.items()):
        if count > MAX_PER_CLUB:
            result.error(f"{count} players from team {club}, max is {MAX_PER_CLUB}")

    # --- 3. Budget ----------------------------------------------------------
    squad_value = sum(p.sells_for for p in squad)
    available = budget if budget is not None else squad_value + bank
    if squad_value > available:
        result.error(
            f"squad costs {format_money(squad_value)} but only "
            f"{format_money(available)} is available "
            f"(over by {format_money(squad_value - available)})"
        )

    # --- 4. Starting XI and formation ---------------------------------------
    starters = [p for p in squad if p.is_starting]
    if len(starters) != STARTING_XI:
        result.error(f"{len(starters)} players in the starting XI, expected {STARTING_XI}")
    else:
        counts = {
            position: sum(1 for p in starters if p.position is position) for position in limits
        }
        for position, (lo, hi) in limits.items():
            if not lo <= counts[position] <= hi:
                result.error(
                    f"starting XI has {counts[position]} {position.short}, "
                    f"must be between {lo} and {hi}"
                )
        formation = (counts[Position.DEF], counts[Position.MID], counts[Position.FWD])
        if formation not in valid_formations(limits):
            result.error(f"formation {'-'.join(map(str, formation))} is not legal")

    # --- 5. Captaincy -------------------------------------------------------
    captains = [p for p in squad if p.is_captain]
    vices = [p for p in squad if p.is_vice_captain]
    if len(captains) != 1:
        result.error(f"{len(captains)} captains, expected exactly 1")
    elif not captains[0].is_starting:
        result.error("captain is not in the starting XI")
    if len(vices) != 1:
        result.error(f"{len(vices)} vice-captains, expected exactly 1")
    elif not vices[0].is_starting:
        result.error("vice-captain is not in the starting XI")
    if captains and vices and captains[0].element_id == vices[0].element_id:
        result.error("captain and vice-captain are the same player")

    # --- 6. Bench order -----------------------------------------------------
    bench = [p for p in squad if not p.is_starting]
    if len(bench) == SQUAD_SIZE - STARTING_XI:
        ranks = [p.bench_rank for p in bench]
        if any(r is None for r in ranks):
            result.warn("bench order is unset; it will be assigned by descending xP")
        elif sorted(r for r in ranks if r is not None) != [1, 2, 3, 4]:
            result.error(f"bench ranks must be exactly 1-4, got {sorted(ranks)}")
        else:
            reserve_gk = [p for p in bench if p.position is Position.GKP]
            if len(reserve_gk) == 1 and reserve_gk[0].bench_rank != 4:
                result.error("the reserve goalkeeper must occupy the last bench slot")

    # --- 7. Player availability ---------------------------------------------
    if elements:
        for p in squad:
            element = elements.get(p.element_id)
            if element is None:
                result.error(f"player {p.element_id} is not in the current player list")
                continue
            if element.status == Availability.UNAVAILABLE or not element.is_transactable:
                result.error(f"{element.name} has left the league and cannot be owned")
            elif element.status == Availability.INJURED and p.is_starting:
                result.warn(f"{element.name} is injured but is in the starting XI")
            elif element.status == Availability.SUSPENDED and p.is_starting:
                result.error(f"{element.name} is suspended and cannot start")
            elif element.status == Availability.DOUBTFUL and p.is_starting:
                chance = element.chance_of_playing_next_round
                if chance is not None and chance <= 50:
                    result.warn(f"{element.name} is only {chance}% likely to play but starts")

    # --- 8. Chip ------------------------------------------------------------
    if chip is not None and chip not in ("wildcard", "freehit", "bboost", "3xc"):
        result.error(f"unknown chip {chip!r}")

    return result


def available_chips(
    bootstrap: Bootstrap, used_chip_names: list[str], gameweek: int
) -> list[str]:
    """Chips that are genuinely usable in ``gameweek``.

    Derived from the live chip list intersected with what the manager has already
    spent. Never assume a chip is available merely because it appears in
    ``bootstrap.chips`` — this season ships two of each, one per half of the
    season, and the first-half set expires unused at the GW19 deadline.
    """
    used = list(used_chip_names)
    available: list[str] = []
    for chip in bootstrap.chips:
        if not chip.start_event <= gameweek <= chip.stop_event:
            continue
        if chip.name in used:
            # Consume one use; a second copy of the same chip in another window
            # remains available.
            used.remove(chip.name)
            continue
        available.append(chip.name)
    return sorted(set(available))

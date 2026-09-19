"""Multi-gameweek squad optimisation.

A mixed-integer linear program over a rolling horizon. **No LLM inference happens
here** — this stage consumes an expected-points vector and emits a squad. Keeping
it deterministic is the whole reason the system can be trusted to auto-submit:
the model supplies judgment, the solver supplies correctness.

See the ``fpl-optimiser`` skill for the formulation and its rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pulp

from ..fpl.rules import (
    DEFAULT_PLAY_LIMITS,
    DEFAULT_SQUAD_REQUIREMENTS,
    HIT_COST,
    MAX_PER_CLUB,
    SQUAD_SIZE,
    STARTING_XI,
)
from ..fpl.schemas import Position
from ..money import selling_price

log = logging.getLogger(__name__)

# Chips that make every transfer free for the gameweek they are played.
CHIPS_FREEING_TRANSFERS = frozenset({"wildcard", "freehit"})

# The vice-captain contributes nothing to expected points under this objective —
# they only score if the captain does not play, which the model does not
# simulate. Without a tiebreak the solver picks one arbitrarily and reruns
# differ, which makes every bug irreproducible. This weight is small enough never
# to distort a real decision and large enough to make the vice the second-best
# starter deterministically.
VICE_TIEBREAK_WEIGHT = 1e-4

# `free[t]` is encoded as an upper bound (`free <= carried + 1`), and it appears
# in the model only inside `hits >= made - free`. So when a gameweek makes no
# transfers, nothing pushes it up and it settles at its lower bound of zero — at
# which point the LP believes a *future* transfer would cost a hit when it would
# not, and plans too conservatively. A tiny reward drives it to its true value,
# min(cap, carried + 1). Far smaller than the churn penalty, so it never makes
# hoarding transfers look attractive on its own.
FREE_TRANSFER_TIEBREAK = 1e-3


class OptimiserError(RuntimeError):
    """The model could not be solved."""


class InfeasibleError(OptimiserError):
    """No legal squad exists under these constraints.

    Almost always bad input rather than a genuinely impossible position: a stale
    price, a wrong bank, an owned player missing from the candidate pool. Relax
    in the documented order rather than silently returning the current squad.
    """


@dataclass(frozen=True)
class Candidate:
    """A player the optimiser may select, with their forecast.

    ``xp`` is indexed by horizon position, not gameweek id: ``xp[0]`` is the
    gameweek being planned.
    """

    element_id: int
    position: Position
    team: int
    now_cost: int  # tenths
    xp: tuple[float, ...]
    sigma: tuple[float, ...] = ()
    owned: bool = False
    purchase_price: int | None = None  # tenths; required when owned
    name: str = ""

    @property
    def sells_for(self) -> int:
        """Selling value in tenths.

        For an unowned player this is ``now_cost`` — which is also the right
        answer for a player bought and sold within the horizon, since no price
        rise has accrued to them yet.
        """
        if not self.owned or self.purchase_price is None:
            return self.now_cost
        return selling_price(self.purchase_price, self.now_cost)

    def value(self, t: int, risk_aversion: float = 0.0) -> float:
        """Risk-adjusted expected points at horizon index ``t``.

        Maximising a point estimate systematically over-picks volatile players —
        the solver chases the upper tail. Subtracting a fraction of the standard
        deviation prefers robust squads while keeping the model linear.
        """
        xp = self.xp[t] if t < len(self.xp) else 0.0
        sd = self.sigma[t] if t < len(self.sigma) else 0.0
        return xp - risk_aversion * sd

    def points(self, t: int) -> float:
        """Unadjusted expected points, for reporting rather than optimising."""
        return self.xp[t] if t < len(self.xp) else 0.0


@dataclass(frozen=True)
class OptimiserConfig:
    horizon: int = 5
    discount: float = 0.85
    risk_aversion: float = 0.2
    bench_weight: float = 0.1

    max_hit: int | None = 4
    """Most points spendable on transfers in any one gameweek. None removes the bound.

    Applied to every gameweek in the horizon, not only the one being submitted,
    so the plan never depends on a future hit the agent would refuse to take.
    """

    churn_penalty: float = 0.05
    """Points charged per transfer, purely to break ties toward stability.

    A free transfer is costless in the objective, so with it at zero the solver
    happily swaps between equally-rated players — burning a transfer that could
    have been banked and producing a different plan on every rerun. The penalty
    is a conservative proxy for the real option cost of churn: the banked
    transfer you give up, and the sell-on fee you lock in. Keep it far below the
    smallest upgrade worth making, so it never blocks a genuine one.
    """

    max_free_transfers: int = 5
    solver_time_limit: int = 60
    squad_requirements: dict[Position, int] = field(
        default_factory=lambda: dict(DEFAULT_SQUAD_REQUIREMENTS)
    )
    play_limits: dict[Position, tuple[int, int]] = field(
        default_factory=lambda: dict(DEFAULT_PLAY_LIMITS)
    )


@dataclass
class GameweekDecision:
    """What the optimiser proposes for one gameweek of the horizon."""

    index: int
    squad: list[int]
    starting: list[int]
    bench: list[int]  # substitution order; reserve goalkeeper last
    captain: int
    vice_captain: int
    transfers_in: list[int]
    transfers_out: list[int]
    hits: int
    bank: int
    free_transfers: int
    expected_points: float

    @property
    def hit_cost(self) -> int:
        return self.hits * HIT_COST


@dataclass
class Plan:
    decisions: list[GameweekDecision]
    objective: float
    """The LP objective: risk-adjusted, discounted, net of hits. Comparable
    across chip options; not a points prediction."""

    chip: str | None = None
    status: str = "Optimal"

    @property
    def this_week(self) -> GameweekDecision:
        return self.decisions[0]

    @property
    def total_expected_points(self) -> float:
        return round(sum(d.expected_points for d in self.decisions), 2)


def optimise(
    candidates: list[Candidate],
    *,
    initial_bank: int,
    initial_free_transfers: int = 1,
    config: OptimiserConfig | None = None,
    chip: str | None = None,
    max_transfers: int | None = None,
) -> Plan:
    """Solve for the best squad over the horizon.

    ``initial_bank`` is in tenths. For a fresh squad — a wildcard, or the start of
    a season — pass candidates with ``owned=False`` and the full budget as the
    bank, along with ``chip="wildcard"``; otherwise buying 15 players costs 14
    hits and the model is infeasible under ``max_hit``.

    ``chip`` forces a chip at horizon index 0. Free Hit is modelled only as free
    transfers — the squad **does not revert** the following gameweek — so a Free
    Hit plan is trustworthy for the current gameweek only.
    """
    cfg = config or OptimiserConfig()
    if not candidates:
        raise OptimiserError("no candidates supplied")

    horizon = max(1, cfg.horizon)
    players = list(candidates)
    index = {c.element_id: c for c in players}
    if len(index) != len(players):
        raise OptimiserError("duplicate element_id in candidate pool")

    ids = [c.element_id for c in players]
    weeks = list(range(horizon))
    clubs = sorted({c.team for c in players})

    problem = pulp.LpProblem("fpl_squad", pulp.LpMaximize)

    squad = pulp.LpVariable.dicts("squad", (ids, weeks), cat="Binary")
    start = pulp.LpVariable.dicts("start", (ids, weeks), cat="Binary")
    cap = pulp.LpVariable.dicts("cap", (ids, weeks), cat="Binary")
    vice = pulp.LpVariable.dicts("vice", (ids, weeks), cat="Binary")
    buy = pulp.LpVariable.dicts("buy", (ids, weeks), cat="Binary")
    sell = pulp.LpVariable.dicts("sell", (ids, weeks), cat="Binary")
    hits = pulp.LpVariable.dicts("hits", weeks, lowBound=0, cat="Integer")
    free = pulp.LpVariable.dicts(
        "free", weeks, lowBound=0, upBound=cfg.max_free_transfers, cat="Integer"
    )
    bank = pulp.LpVariable.dicts("bank", weeks, lowBound=0, cat="Integer")

    # A chip applies only to the gameweek being planned.
    transfers_are_free = chip in CHIPS_FREEING_TRANSFERS
    bench_boost = chip == "bboost"
    captain_multiplier = 3 if chip == "3xc" else 2

    # ---------------------------------------------------------------- objective
    terms = []
    for t in weeks:
        discount = cfg.discount**t
        # The captain's extra share on top of their starting appearance.
        extra = (captain_multiplier - 1) if t == 0 else 1
        # Under Bench Boost the bench scores in full; otherwise it earns a small
        # weight so the solver does not fill it with £4.0m players who never appear.
        bench_share = 1.0 if (bench_boost and t == 0) else cfg.bench_weight

        for c in players:
            value = c.value(t, cfg.risk_aversion)
            i = c.element_id
            terms.append(discount * value * start[i][t])
            terms.append(discount * value * extra * cap[i][t])
            terms.append(discount * VICE_TIEBREAK_WEIGHT * value * vice[i][t])
            terms.append(discount * bench_share * value * (squad[i][t] - start[i][t]))
        terms.append(-HIT_COST * hits[t])
        terms.append(FREE_TRANSFER_TIEBREAK * free[t])

        # Discourage pointless churn. Suppressed when a chip already makes
        # transfers free, since a wildcard is *supposed* to rebuild the squad.
        if not (transfers_are_free and t == 0):
            terms.append(-cfg.churn_penalty * pulp.lpSum(buy[i][t] for i in ids))

    problem += pulp.lpSum(terms)

    # -------------------------------------------------------------- constraints
    for t in weeks:
        problem += pulp.lpSum(squad[i][t] for i in ids) == SQUAD_SIZE
        problem += pulp.lpSum(start[i][t] for i in ids) == STARTING_XI

        for position, required in cfg.squad_requirements.items():
            members = [i for i in ids if index[i].position is position]
            problem += pulp.lpSum(squad[i][t] for i in members) == required

        for position, (lo, hi) in cfg.play_limits.items():
            members = [i for i in ids if index[i].position is position]
            problem += pulp.lpSum(start[i][t] for i in members) >= lo
            problem += pulp.lpSum(start[i][t] for i in members) <= hi

        for club in clubs:
            members = [i for i in ids if index[i].team == club]
            problem += pulp.lpSum(squad[i][t] for i in members) <= MAX_PER_CLUB

        problem += pulp.lpSum(cap[i][t] for i in ids) == 1
        problem += pulp.lpSum(vice[i][t] for i in ids) == 1

        for i in ids:
            problem += start[i][t] <= squad[i][t]
            problem += cap[i][t] <= start[i][t]
            problem += vice[i][t] <= start[i][t]
            problem += cap[i][t] + vice[i][t] <= 1
            problem += buy[i][t] + sell[i][t] <= 1

            # Continuity: held now iff held before, plus buys, minus sales.
            previous = int(index[i].owned) if t == 0 else squad[i][t - 1]
            problem += squad[i][t] == previous + buy[i][t] - sell[i][t]

        # Budget, entirely in integer tenths.
        spend = pulp.lpSum(buy[i][t] * index[i].now_cost for i in ids)
        raised = pulp.lpSum(sell[i][t] * index[i].sells_for for i in ids)
        previous_bank = initial_bank if t == 0 else bank[t - 1]
        problem += bank[t] == previous_bank + raised - spend

        made = pulp.lpSum(buy[i][t] for i in ids)

        # Free transfers. `free[t]` is driven to the lower of its two upper
        # bounds by the objective's preference for fewer hits, which is what
        # makes this a correct linear encoding of min(cap, carried + 1).
        if t == 0:
            problem += free[t] == min(initial_free_transfers, cfg.max_free_transfers)
        elif transfers_are_free and t == 1:
            # A wildcard or free hit banks nothing: you return to one free
            # transfer regardless of how many the chip allowed. Without this the
            # carry formula computes free[0] - 15 buys = -14 and the model is
            # infeasible, which presents as "no legal squad exists".
            problem += free[t] <= 1
        else:
            carried = free[t - 1] - pulp.lpSum(buy[i][t - 1] for i in ids) + hits[t - 1]
            problem += free[t] <= carried + 1

        if transfers_are_free and t == 0:
            problem += hits[t] == 0
        else:
            problem += hits[t] >= made - free[t]
            if cfg.max_hit is not None:
                problem += hits[t] * HIT_COST <= cfg.max_hit

        if max_transfers is not None:
            problem += made <= max_transfers

    _solve(problem, cfg.solver_time_limit)
    objective = float(pulp.value(problem.objective) or 0.0)

    decisions = [
        _decision_for(
            t,
            ids=ids,
            index=index,
            squad=squad,
            start=start,
            cap=cap,
            vice=vice,
            buy=buy,
            sell=sell,
            hits=hits,
            free=free,
            bank=bank,
            config=cfg,
            captain_multiplier=captain_multiplier if t == 0 else 2,
            bench_boost=bench_boost and t == 0,
        )
        for t in weeks
    ]

    return Plan(decisions=decisions, objective=round(objective, 3), chip=chip)


def _solve(problem: pulp.LpProblem, time_limit: int) -> None:
    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit)
    problem.solve(solver)
    status = pulp.LpStatus[problem.status]
    if problem.status == pulp.LpStatusInfeasible:
        raise InfeasibleError(
            "no legal squad exists under these constraints — check the bank, the "
            "prices, and that every owned player is present in the candidate pool"
        )
    if problem.status != pulp.LpStatusOptimal:
        # A timeout is a failed stage, never a result to be trusted.
        raise OptimiserError(f"solver returned {status!r}")


def _binary(variable: pulp.LpVariable) -> bool:
    """Round a solver binary.

    CBC returns binaries as 0.9999999, so rounding is required. The rounded
    solution is then re-checked by the independent validator in ``fpl.rules`` —
    the relaxation is never trusted on its own.
    """
    value = variable.value()
    return value is not None and round(value) == 1


def _decision_for(
    t: int,
    *,
    ids: list[int],
    index: dict[int, Candidate],
    squad: dict,
    start: dict,
    cap: dict,
    vice: dict,
    buy: dict,
    sell: dict,
    hits: dict,
    free: dict,
    bank: dict,
    config: OptimiserConfig,
    captain_multiplier: int,
    bench_boost: bool,
) -> GameweekDecision:
    in_squad = [i for i in ids if _binary(squad[i][t])]
    starting = [i for i in in_squad if _binary(start[i][t])]
    benched = [i for i in in_squad if i not in starting]

    captain = next((i for i in ids if _binary(cap[i][t])), starting[0])
    vice_captain = next(
        (i for i in ids if _binary(vice[i][t])),
        next((i for i in starting if i != captain), starting[0]),
    )

    # Bench order is assigned here rather than modelled as a variable: it does
    # not affect the objective, so optimising it would add cost for nothing.
    # Outfield substitutes rank by expected points; the reserve goalkeeper always
    # takes the final slot.
    outfield = sorted(
        (i for i in benched if index[i].position is not Position.GKP),
        key=lambda i: (index[i].value(t, config.risk_aversion), -i),
        reverse=True,
    )
    reserve_gk = [i for i in benched if index[i].position is Position.GKP]
    bench_order = outfield + reserve_gk

    # Reported expected points are unadjusted — risk aversion shapes the choice
    # but should not distort what we claim the squad will score.
    expected = sum(index[i].points(t) for i in starting)
    expected += index[captain].points(t) * (captain_multiplier - 1)
    if bench_boost:
        expected += sum(index[i].points(t) for i in bench_order)

    return GameweekDecision(
        index=t,
        squad=in_squad,
        starting=starting,
        bench=bench_order,
        captain=captain,
        vice_captain=vice_captain,
        transfers_in=[i for i in ids if _binary(buy[i][t])],
        transfers_out=[i for i in ids if _binary(sell[i][t])],
        hits=round(hits[t].value() or 0),
        bank=round(bank[t].value() or 0),
        free_transfers=round(free[t].value() or 0),
        expected_points=round(expected, 2),
    )

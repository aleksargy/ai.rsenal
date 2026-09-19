---
name: fpl-optimiser
description: The multi-gameweek integer program that picks the squad — decision variables, objective, constraint formulation, chip and hit modelling, and the numerical pitfalls that silently produce illegal squads. Load before writing or modifying anything in src/arsenal/optimizer/.
---

# The optimiser

A mixed-integer linear program over a rolling horizon. This stage is **pure
arithmetic with no LLM inference** — it consumes the xP vector and emits the
squad. Keeping it deterministic is the whole reason the system can be trusted to
auto-submit: the model supplies judgment, the solver supplies correctness.

PuLP with CBC is the default (pure Python, bundled solver, adequate at this
scale — a 660-player, 5-gameweek model solves in seconds). OR-Tools CP-SAT is a
drop-in alternative if the model grows.

## Why an LP rather than the LLM

FPL squad selection is a **multi-dimensional knapsack with side constraints**:
budget, squad-size, per-position counts, a 3-per-club cap, formation legality,
and transfer costs, optimised jointly over several gameweeks. Language models are
unreliable at exactly this — they produce squads that are £0.3m over budget or
carry four Arsenal players, and they do it *confidently*.

The division of labour is the core architectural commitment of this repo:

- The **LLM** decides *what a player is worth* — judgment under uncertainty over
  unstructured evidence, which a solver cannot do at all.
- The **solver** decides *which players to own* — combinatorial optimisation
  under hard constraints, which an LLM cannot do reliably.

Never blur this. An LLM "adjusting" the solver's output re-introduces exactly the
errors the solver exists to eliminate. If the model disagrees with the solver,
the correct fix is to change the **xP inputs** and re-solve — that is a
principled edit with a traceable cause, where a manual override is not.

## Decision variables

Over horizon `t ∈ {0..H-1}` (default H=5) and players `p`:

| Variable | Domain | Meaning |
|---|---|---|
| `squad[p,t]` | binary | p is in the 15 at gameweek t |
| `start[p,t]` | binary | p is in the starting XI |
| `cap[p,t]` | binary | p is captain |
| `vice[p,t]` | binary | p is vice-captain |
| `buy[p,t]` | binary | p transferred in at t |
| `sell[p,t]` | binary | p transferred out at t |
| `hits[t]` | integer ≥ 0 | point-costing transfers at t |
| `ft[t]` | integer 0..5 | free transfers carried into t |
| `chip[c,t]` | binary | chip c played at t |
| `bench_order[p,t]` | — | assigned post-solve, not a variable |

Bench order is deliberately **not** in the LP: it does not affect expected points
under the objective, and modelling it adds variables for nothing. Assign it
afterwards by descending xP, reserve GK last.

## Objective

Maximise discounted expected points net of transfer costs:

```
Σ_t  γ^t · [ Σ_p ( start[p,t] · xP[p,t] + cap[p,t] · xP[p,t] · (mult_t − 1) )
           + bench_boost_contribution[t]
           + λ · Σ_p start[p,t] · bench_value[p,t] ]
     − 4 · Σ_t hits[t]
```

- `γ ≈ 0.85` — discount future gameweeks. Near-term xP is more reliable and
  plans rarely survive to gameweek 5 intact.
- `mult_t` is 2 normally, 3 under Triple Captain.
- `λ` is a small weight on bench quality, preventing the solver from filling the
  bench with £4.0m players who never score. Keep it small — bench points are
  real but secondary outside Bench Boost.
- Hits are a **−4 linear penalty**, which is exactly correct; do not soften it.

### Uncertainty

Point-estimate maximisation systematically over-picks volatile players — the
solver chases the upper tail. Two mitigations, both cheap:

- **Risk-adjust the input:** optimise `xP − κ·σ` with small `κ` (≈0.2). Simple,
  effective, keeps the model linear.
- **Scenario-average:** sample xP vectors from their distributions, solve each,
  and pick the squad most frequently optimal. More faithful, materially slower.

Default to risk adjustment; reserve scenarios for chip decisions, where the
stakes justify the cost.

## Constraints

Transcribe from `fpl-rules` — that file is the authority, this is the encoding.

```python
# Squad composition
for t in T:
    lpSum(squad[p, t] for p in P) == 15
    for pos, n in {GKP: 2, DEF: 5, MID: 5, FWD: 3}.items():
        lpSum(squad[p, t] for p in P if pos_of[p] == pos) == n

    # Starting XI
    lpSum(start[p, t] for p in P) == 11
    lpSum(start[p, t] for p in P if pos_of[p] == GKP) == 1
    for pos, (lo, hi) in {DEF: (3, 5), MID: (2, 5), FWD: (1, 3)}.items():
        lo <= lpSum(start[p, t] for p in P if pos_of[p] == pos) <= hi

    # Club limit
    for club in clubs:
        lpSum(squad[p, t] for p in P if team_of[p] == club) <= 3

    # Linking — you cannot start or captain a player you do not own
    for p in P:
        start[p, t] <= squad[p, t]
        cap[p, t] <= start[p, t]
        vice[p, t] <= start[p, t]
        cap[p, t] + vice[p, t] <= 1  # must be different players
    lpSum(cap[p, t] for p in P) == 1
    lpSum(vice[p, t] for p in P) == 1
```

### Squad transition and budget

```python
# Continuity: you hold p at t iff you held it at t-1, minus sales, plus buys
squad[p, t] == squad[p, t - 1] + buy[p, t] - sell[p, t]
buy[p, t] + sell[p, t] <= 1  # never both in one gameweek

# Budget, in INTEGER TENTHS
bank[t] == bank[t - 1] + lpSum(sell[p, t] * selling_price[p] for p in P) - lpSum(
    buy[p, t] * now_cost[p] for p in P
)
bank[t] >= 0
```

`selling_price` uses the 50%-of-profit rule from `fpl-rules`, computed per player
from its **purchase price** — a constant read from `my-team/`, not a variable.
Buying is always at full `now_cost`.

### Free transfers and hits

```python
ft[t] == min(5, ft[t - 1] - used[t - 1] + 1)
hits[t] >= lpSum(buy[p, t] for p in P) - ft[t]
hits[t] >= 0
```

`min` is not linear. Encode the cap as `ft[t] <= 5` plus
`ft[t] <= ft[t-1] - used[t-1] + 1`, and let the objective's preference for
cheaper transfers drive it to the bound. Verify this holds on a case where
banking is optimal — it is a classic source of a silently wrong plan.

Under Wildcard or Free Hit, `hits[t] = 0` regardless of transfer count.

### Chips

```python
for c in chips:
    lpSum(chip[c, t] for t in T) <= available[c]   # from entry history
for t in T:
    lpSum(chip[c, t] for c in chips) <= 1          # one chip per gameweek
    chip[c, t] == 0  unless start_event[c] <= gw(t) <= stop_event[c]
```

Bench Boost adds bench xP to the objective at `t`; Triple Captain sets
`mult_t = 3`. Free Hit needs care: the squad at `t+1` must revert to the squad at
`t−1`, so model the Free Hit gameweek's squad as a **separate variable set** that
does not participate in the continuity constraint.

Chip *timing* is where a horizon of 5 is too short — Bench Boost wants a double
gameweek that may be 10 weeks away. Handle chip timing as a **separate,
longer-horizon, coarser analysis** (fixture-density scan over the remaining
season) that hands the main LP a recommended window, rather than inflating H.

## Numerical pitfalls

1. **Integer tenths everywhere.** `now_cost = 75` is £7.5m. Floats produce
   `bank = -0.000001`, an infeasible model, and an aborted deadline run.
2. **Solver tolerance.** CBC returns binaries as `0.9999999`. Round with
   `int(round(v))` and **assert the rounded solution still satisfies every
   constraint** — never trust the relaxation.
3. **Infeasible ≠ no good move.** Infeasibility almost always means bad input:
   a stale price, a wrong bank, a player already sold. Log the full model state
   and relax in the documented order (drop chip → cap transfers at 1 → 0) rather
   than silently returning the current squad.
4. **Degenerate optima.** Many squads tie on xP. Break ties deterministically
   (secondary objective on total xP, then on player id) so reruns with identical
   inputs produce identical plans. Non-determinism here makes every bug
   irreproducible.
5. **Unowned-player prices.** `selling_price` is only defined for owned players.
   For everyone else it is `now_cost`. Conflating them inflates the budget and
   produces a plan the server rejects.
6. **Set a solver time limit** (~60s) and treat a timeout as a failed stage that
   falls back to a reduced horizon, not as a valid answer.

## Validation

The solver's output is re-checked by an **independently written** validator
(`src/arsenal/fpl/rules.py`) that does not import the optimiser's constraint
code. Shared code means shared bugs, and a constraint bug that exists in both
places is invisible precisely when it matters.

Test the optimiser against hand-constructed fixtures with known optimal answers,
including: a forced hit, a banked-transfer scenario, a budget-binding case, a
3-per-club-binding case, and a sell-on-fee case where naive budgeting overspends.

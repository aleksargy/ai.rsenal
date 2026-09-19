---
name: fpl-rules
description: Authoritative Fantasy Premier League game rules — scoring table, squad constraints, transfer and hit arithmetic, sell-on fee, chip mechanics, price changes. Load this BEFORE reasoning about any squad legality, points calculation, transfer cost, or chip decision. Never answer an FPL rules question from memory; the game changes rules most seasons and the numbers here are pulled from the live game engine.
---

# FPL Rules (authoritative)

Every number in this file comes from `bootstrap-static.game_config`, which is the live
scoring/rules config the FPL engine itself runs on. Prefer it over any blog, any
tutorial, and over your own recollection — **scoring rules change most seasons.**

Refresh it with `uv run arsenal rules --refresh`, which rewrites
`data/reference/game_config.json` and flags any diff against the values below.

> **Staleness check.** If `data/reference/game_config.json` is older than 7 days,
> refresh before relying on this file. A rule change mid-season is rare but not
> unheard of, and an agent that auto-submits must not act on a stale rulebook.

## Scoring

Points per event, by position. `GKP=1, DEF=2, MID=3, FWD=4` are the `element_type` ids.

| Event | GKP | DEF | MID | FWD |
|---|---|---|---|---|
| Playing 1–59 min (`short_play`) | 1 | 1 | 1 | 1 |
| Playing 60+ min (`long_play`) | 2 | 2 | 2 | 2 |
| Goal scored | 10 | 6 | 5 | 4 |
| Assist | 3 | 3 | 3 | 3 |
| Clean sheet | 4 | 4 | 1 | 0 |
| Every 2 goals conceded | −1 | −1 | 0 | 0 |
| Every 3 shot saves | 1 | — | — | — |
| Penalty save | 5 | — | — | — |
| Penalty miss | −2 | −2 | −2 | −2 |
| Yellow card | −1 | −1 | −1 | −1 |
| Red card | −3 | −3 | −3 | −3 |
| Own goal | −2 | −2 | −2 | −2 |
| **Defensive contribution** | 0 | **2** | **2** | **2** |
| Bonus | 1–3 | 1–3 | 1–3 | 1–3 |

### Defensive contribution — the rule most models get wrong

A flat **2 points**, awarded at most once per match, when a player crosses a
per-position threshold of defensive actions:

- **Defenders:** 10+ combined clearances, blocks, interceptions and tackles (CBIT).
- **Midfielders and forwards:** 12+ combined CBIT **plus ball recoveries**.
- **Goalkeepers:** not eligible (`defensive_contribution.GKP = 0`).

Two consequences that matter for forecasting:

1. It is a **threshold**, not a rate. Expected DC points are
   `2 × P(actions ≥ threshold)`, **not** `2 × E[actions] / threshold`. A player
   averaging 9.5 CBIT is worth far less than one averaging 10.5 — model the
   probability, not the mean. Use the per-90 distribution across recent starts.
2. It structurally repriced cheap defensive midfielders and ball-winning
   full-backs. `defensive_contribution` and `defensive_contribution_per_90` are
   live fields on every element — use them directly rather than reconstructing
   from the component stats.

> The **2 points** and the GKP exclusion are confirmed from `game_config.scoring`.
> The **10 / 12 thresholds** are enforced engine-side and are *not* exposed in any
> API payload. Treat them as the documented values, but validate empirically: for
> any player with known `defensive_contribution` counts, check the threshold that
> reproduces their observed awards. If validation fails, the thresholds moved —
> stop and flag it rather than forecasting on a broken assumption.

### Bonus (BPS)

Top three BPS scorers in each match get 3, 2, 1. Ties share the higher award
(two players tied on top → both get 3, next gets 1). `bps` is the raw score;
`bonus` is the awarded points. For in-progress gameweeks, provisional bonus is
derived from live BPS and can change until the match is `finished_provisional`.

### Not scoring

`mng_*` (manager points), `bps`, `influence`, `creativity`, `threat`, `ict_index`,
`starts`, and all `expected_*` fields carry **0 points**. They are *predictors*,
never *scorers*. In particular, all `mng_*` values are 0 this season, which
confirms the **Assistant Manager chip has been removed** — the chip list in
`bootstrap-static.chips` is the authority on what actually exists.

## Squad constraints

Every one of these is a hard constraint in the optimiser. A violation is not a
penalty, it is a rejected submission.

| Rule | Value | Config key |
|---|---|---|
| Squad size | 15 | `squad_squadsize` |
| Starting XI | 11 | `squad_squadplay` |
| Budget (initial) | £100.0m | `squad_total_spend` = 1000 |
| Max players per real club | 3 | `squad_team_limit` |
| Goalkeepers | exactly 2 (1 starts) | `element_types[1]` |
| Defenders | exactly 5 (3–5 start) | `element_types[2]` |
| Midfielders | exactly 5 (2–5 start) | `element_types[3]` |
| Forwards | exactly 3 (1–3 start) | `element_types[4]` |

Prices are stored in **tenths of a million**. `now_cost = 75` means £7.5m
(`ui_currency_multiplier` = 10). **Do all money arithmetic in integer tenths.**
Floating-point pounds will eventually produce an off-by-0.1 squad that the API
rejects at the deadline, which is the worst possible time to find out.

Valid formations follow from the min/max play counts: 3-4-3, 3-5-2, 4-3-3,
4-4-2, 4-5-1, 5-3-2, 5-4-1, 5-2-3, 3-4-3. Do not hardcode this list — derive it
from `squad_min_play`/`squad_max_play` so it survives a rule change.

Bench is ordered: positions 12, 13, 14 are outfield substitutes in priority
order, and position 15 is the reserve goalkeeper. Automatic substitutions only
fire for players who recorded **0 minutes**, and only if the resulting XI is
still a legal formation.

## Transfers

- **1 free transfer per gameweek.**
- Unused free transfers bank, up to a maximum of **5**
  (`max_extra_free_transfers` = 4, plus the current week's 1).
- Each transfer beyond your free allowance costs **−4 points**, deducted from
  that gameweek's score.
- Hard cap of **20** transfers in a single gameweek (`transfers_cap`).
- Wildcard and Free Hit make all transfers free for that gameweek.

### Sell-on fee — get this exactly right

`element_sell_at_purchase_price` is **false** and `transfers_sell_on_fee` is
**0.5**: you keep only half of any price rise, rounded **down** to £0.1m.

Compute it in integer tenths, never in floats:

```python
def selling_price(purchase_price: int, now_cost: int) -> int:
    """All arguments and the return value are in tenths of a million."""
    if now_cost <= purchase_price:
        return now_cost  # losses are taken in full
    return purchase_price + (now_cost - purchase_price) // 2
```

Worked examples:

- Bought 70, now 75 → rise 5 → keep 2 → **sells for 72**, not 75.
- Bought 40, now 43 → rise 3 → keep 1 → **sells for 41**.
- Bought 90, now 86 → fell → **sells for 86** (full loss).

The consequence: **your squad value is not your selling value.** The optimiser
must budget against the sum of *selling* prices plus bank, and it needs each
player's original `purchase_price` — available only from the authenticated
`/api/my-team/{id}/` endpoint, never from public data. If you cannot read
purchase prices, you cannot compute a legal budget; fail the run rather than
guessing.

## Chips

The live list is `bootstrap-static.chips`. This season it contains **two full
sets**, one usable in GW1–19 and one in GW20–38:

| Chip | Effect | Availability |
|---|---|---|
| Wildcard | Unlimited free transfers this GW; squad changes are permanent | 2× (one per half) |
| Free Hit | Unlimited free transfers for **one GW only**; squad reverts next GW | 2× (one per half) |
| Bench Boost | All 15 players score | 2× (one per half) |
| Triple Captain | Captain scores 3× instead of 2× | 2× (one per half) |

Rules that constrain automation:

- **One chip per gameweek.** Never stack.
- First-half chips **expire unused at the GW19 deadline** — they do not roll
  over. An unused first-half chip approaching GW19 is a *use-it-or-lose-it*
  asset and the agent should escalate its priority sharply as the deadline nears.
- A chip is **irrevocable once the deadline passes**, though it can be cancelled
  before the deadline.
- Wildcard does not bank free transfers: you return to 1 FT the following week.
- Free Hit reverts your squad to exactly its pre-Free-Hit state, including
  purchase prices — the one-week squad has no lasting effect on team value.

Derive availability from the manager's actual chip history
(`/api/entry/{id}/history/` → `chips`) intersected with `chips[].start_event` /
`stop_event`. Never assume a chip is available because it is in the list.

## Price changes

Prices move at roughly **01:30 UTC** daily, driven by net transfer momentum
relative to ownership. A player can move at most £0.1m per day.

This season exposes three fields that remove most of the guesswork:
`price_change_projections`, `price_change_hourly_rate`, and
`price_change_locked_until`. Prefer these over third-party price-predictor
scraping.

Price movement is a **second-order** consideration. Chasing a £0.1m rise is worth
0.1 of team value; a wrong transfer is worth −4 points plus the opportunity cost.
Only let price pressure break a tie between options the forecast already rates as
near-equal — never let it drive the decision.

## Deadlines

Deadline is **90 minutes before the first kickoff** of the gameweek, in
`events[].deadline_time` (ISO 8601, always UTC — `game_settings.timezone` is
`UTC`). Everything at the boundary must be UTC-aware; a naive local timestamp on
a machine in BST will silently submit an hour late.

Only these are locked at the deadline: transfers, captain, vice-captain, starting
XI, bench order, chip activation. After it passes, the gameweek is immutable.

Treat the deadline as **T-minus** and schedule against it, never against a fixed
wall-clock cron, because deadlines move for midweek and holiday rounds.

## Invariants to assert before any submission

Check all of these mechanically. Any failure aborts the run — never submit a
squad that fails one, and never "fix it up" with a heuristic.

1. Exactly 15 players; exactly 2/5/5/3 by position.
2. No more than 3 players from any one `team`.
3. `sum(selling_price) + bank ≤ available_budget`, all in integer tenths.
4. Starting XI is 11 players with a formation legal under the min/max play rules.
5. Exactly one captain and one vice-captain, both in the starting XI, and distinct.
6. At most one chip active, and that chip is genuinely unused and in-window.
7. Transfer count ≤ `transfers_cap`, and the computed hit cost matches the
   free-transfer balance read back from the API — not the one you predicted.
8. No player with `status = 'u'` (unavailable/left the league) is in the squad.

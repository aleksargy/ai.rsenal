# ai.rsenal

An autonomous Fantasy Premier League manager. It researches, decides, submits,
and then tells you what it did and why — so a deadline never passes with an
injured captain and three free transfers rotting in the bank.

> **Status: M4 (research).** Read client, schemas, rules engine, validator, the
> multi-gameweek integer program, a bottom-up expected-points model, and the
> evidence pipeline are built and tested. The forecast beats a points-per-game
> baseline on every backtested gameweek. The executor is specified but not
> implemented. See [the roadmap](#roadmap).

## The idea

> **The LLM supplies judgment. The solver supplies correctness.**

Most "AI picks your FPL team" projects ask a language model for a squad and get
one that is £0.3m over budget with four Arsenal players, stated with total
confidence. FPL squad selection is a multi-dimensional knapsack with side
constraints, and language models are bad at exactly that.

So the work is split along the line where each side is strong:

- **LLM agents** decide *what a player is worth*. They read press conferences,
  weigh conflicting injury reports, judge rotation risk, and mine creator and
  community sources — unstructured judgment a solver cannot do at all. They emit
  an expected-points forecast with explicit uncertainty.
- **An integer program** decides *which players to own*. Budget, squad size,
  position counts, the 3-per-club cap, formation legality, free transfers and
  −4 hits, optimised jointly over a rolling multi-gameweek horizon. Provably
  correct where the model is merely plausible.

An independently written validator then re-checks every rule before anything is
submitted — deliberately sharing no code with the optimiser, because a bug
duplicated in both is invisible exactly when it matters.

## Architecture

```
snapshot → research → forecast → optimise → validate → execute → notify
              │                                                      │
     ┌────────┼────────┬──────────────┐                          Telegram
     ▼        ▼        ▼              ▼
 news-scout  stats  fixture-analyst  community-scout
  (Tier 1/3) (T2)      (T1/T2)          (Tier 4)
     └──────────── Evidence[] ──────────────┘
          tiered · sourced · timestamped
```

Each stage writes a typed artifact to `data/runs/{gw}/`, so a failed run resumes
from the last good stage and every decision is reproducible from its inputs.

Full design in [docs/SPEC.md](docs/SPEC.md).

## The catch: there is no write API

This is worth knowing before you invest in the repo.

- `users.premierleague.com` — the login host every FPL tutorial and library still
  documents — **no longer resolves.** Code written against it fails with DNS
  errors, not auth errors, which is a confusing way to find out.
- Auth moved to `account.premierleague.com`, a bot-protected SSO that returns
  **403 to any non-browser client.** There is no scriptable credential flow.
- Reads are wide open and unusually rich.

So writes replay a session harvested from a real browser. Sessions expire, and
are partly fingerprint-bound, which makes replaying one from a CI runner the most
likely failure point in the system.

The executor is therefore built to degrade rather than break:

1. **Direct API** with replayed cookies — fast, cheap
2. **Playwright browser replay** — slower, survives some checks the direct call fails
3. **Advisory** — no write; full recommendation, flagged for manual action

Full auto when the session is healthy, recommendations when it is not, and a
Telegram message either way that says plainly which happened.

## Quick start

```bash
uv venv
uv pip install -e ".[dev]"

uv run arsenal doctor      # probe every endpoint, check schema drift and session
uv run arsenal rules       # the live scoring table, straight from the game engine
uv run arsenal deadline    # next deadline and which pipeline stage is due
uv run arsenal status      # your squad, bank, free transfers, chips (needs a session)
uv run arsenal plan        # solve for the optimal squad and print it
uv run arsenal forecast    # highest expected-points players, with components
uv run arsenal backtest    # score the forecast against completed gameweeks
uv run arsenal research    # gather team news and show how it moves the forecast
```

`plan` needs no credentials if you pass `--fresh`, which builds a squad from a
full £100.0m rather than optimising a team it cannot read:

```bash
uv run arsenal plan --fresh --horizon 5
uv run arsenal plan --chip bboost      # evaluate a specific chip
uv run arsenal plan --max-hit 0        # forbid hits entirely
```

Every plan is cross-checked by the independent validator before it is printed —
if the solver and the rulebook ever disagree, the command fails loudly rather
than showing you an illegal squad.

`doctor` needs no credentials and is the right first command — it tells you
whether a failure is your code or the API having moved underneath you.

## Does the forecast work?

`arsenal backtest` predicts each gameweek using only data from strictly before
it, then scores the result. Current season to date:

| | model | baseline |
|---|---|---|
| Mean absolute error | **2.35** | 2.98 |
| Rank correlation | **+0.251** | +0.175 |

The model's ten highest-rated players outscored the field by **+1.84 points per
gameweek**. The baseline is each player's points per gameweek so far — "just pick
whoever has been scoring", which is the bar any model has to clear to justify its
complexity.

Two things the backtest is careful about, because both would make the numbers
look good and mean nothing:

- **Injury status is disabled.** `status` describes today, so using it to predict
  a past gameweek tells the model who got injured. Live forecasts keep it — that
  information is genuinely available before a deadline.
- **Season totals are never read.** Everything in `bootstrap.elements` is
  cumulative to now and silently includes the gameweek being predicted. Rates are
  rebuilt from per-gameweek history truncated before the target.

Caveat worth stating plainly: this is a handful of gameweeks. Treat it as
directional. Rank correlation is the number that matters — squad selection needs
players ordered correctly, not their totals predicted exactly.

## Evidence and tiers

Research feeds the forecast through a tier system that is **enforced in code**,
not left to judgement — because the most damaging failure mode of a system like
this is a confident YouTuber's "nailed on to start" moving a number on its own.

| Tier | What | May move a forecast |
|---|---|---|
| 1 — Fact | FPL API status, completed match data, club statements | Yes, up to ruling a player out entirely |
| 2 — Measured | Understat / FBref / Opta underlying numbers | Yes, bounded |
| 3 — Reported | Press conferences, named beat reporters | Yes, bounded and floored |
| 4 — Opinion | Creators, Reddit, blog predictions | **Never** |

Tier 4 earns its place two other ways: surfacing claims to verify at a higher
tier, and showing what the field is doing. When a Tier 4 source *quotes* a
primary — a creator relaying a press conference — the claim is promoted to
Tier 3, because the press conference is the evidence and the creator is not.

Other rules the pipeline enforces:

- **Every claim needs a resolving URL.** A recollection is not evidence, and no
  confidence score makes it one.
- **Hedging is preserved.** "Should be available" is not "is available"; the
  hedge halves the claim's force rather than being flattened away.
- **Repetition is not corroboration.** Five outlets reporting one press
  conference deduplicate to one record.
- **Conflicts widen uncertainty rather than picking a winner.** Two reporters
  disagreeing is itself the finding.
- **Ambiguous names are dropped, never guessed.** Misattributing a claim to the
  wrong player corrupts a forecast silently.

## Configuration

Policy lives in `config.yaml` (version-controlled, so a bad season is traceable
to the setting that caused it). Secrets live in the environment.

| Variable | Needed for |
|---|---|
| `FPL_TEAM_ID` | Everything team-specific |
| `FPL_SESSION_JSON` | Authenticated reads and all writes |
| `ANTHROPIC_API_KEY` | Research agents |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Notifications |
| `YOUTUBE_API_KEY` | Creator transcripts |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | r/FantasyPL |

Copy `.env.example` to `.env` to start.

## Claude skills

`.claude/skills/` holds the domain knowledge, so an agent working in this repo
reasons from verified facts rather than recollection:

| Skill | Covers |
|---|---|
| `fpl-rules` | Scoring, squad constraints, hits, sell-on fee, chips — pulled from the live game engine |
| `fpl-api` | Endpoints, payload shapes, the post-2025 auth model, the write path |
| `fpl-research` | Source tiering, the `Evidence` schema, turning claims into expected points |
| `fpl-optimiser` | The integer program: variables, objective, constraints, numerical pitfalls |
| `fpl-deadline-run` | The T-minus runbook, abort conditions, degradation and recovery |

`.claude/agents/` defines the research fan-out: `fpl-news-scout`,
`fpl-stats-analyst`, `fpl-fixture-analyst`, `fpl-community-scout`.

## Notes for contributors

**All money is integer tenths of a million.** `now_cost = 75` is £7.5m. No floats
anywhere near a price — FPL budgets bind exactly, so a float representation
eventually yields a bank of `-1e-9`, an infeasible model, and an aborted run at
the deadline.

**Selling price is not current price.** FPL takes half your profit, rounded down.
A player bought at 70 and now worth 75 sells for 72. Budget against selling
value, which needs purchase prices from the authenticated `my-team/` endpoint.

**Defensive contribution is a threshold, not a rate.** Expected points are
`2 × P(actions ≥ threshold)`, not `2 × mean / threshold`. A player averaging 9.5
actions is worth far less than one averaging 10.5.

**And `defensive_contribution` is the action count, not points.** A player with
`defensive_contribution: 7` earned **0** points, not 7. It is the tally the
threshold applies to: `CBIT + tackles` for defenders, plus `recoveries` for
midfielders and forwards. Thresholds of 10 and 12 are verified empirically
against the engine's own `explain` breakdown over 1,236 player-gameweeks — no
overlap, award always exactly 2 points. Forwards cleared it zero times.

**Never trust a remembered rule.** Scoring changes most seasons. `game_config` in
`bootstrap-static` is the live config the FPL engine runs on — `arsenal rules`
prints it.

## Roadmap

- [x] **M1** Read client, schemas, rules engine, independent validator, CLI, tests
- [x] **M2** Integer program; `arsenal plan` prints a legal optimal squad
- [x] **M3** Bottom-up expected points, backtested against completed gameweeks
- [x] **M4** Source adapters, evidence tiering, and LLM claim extraction
- [ ] **M5** Session management and the three-layer executor
- [ ] **M6** GitHub Actions scheduling, Telegram, full autonomy

M1–M3 give a system that offers good advice with zero auth risk. M5–M6 add hands.
The breakable part is built last, on top of something already useful.

## Licence

MIT

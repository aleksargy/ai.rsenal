# ai.rsenal

An autonomous Fantasy Premier League manager. It researches, decides, submits,
and then tells you what it did and why — so a deadline never passes with an
injured captain and three free transfers rotting in the bank.

> **Status: M2 (optimiser).** Read client, schemas, rules engine, validator and
> the multi-gameweek integer program are built and tested — `arsenal plan` solves
> a legal optimal squad in ~2.5s. The forecast is still a placeholder; research
> agents and the executor are specified but not implemented. See
> [the roadmap](#roadmap).

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
CBIT is worth far less than one averaging 10.5.

**Never trust a remembered rule.** Scoring changes most seasons. `game_config` in
`bootstrap-static` is the live config the FPL engine runs on — `arsenal rules`
prints it.

## Roadmap

- [x] **M1** Read client, schemas, rules engine, independent validator, CLI, tests
- [x] **M2** Integer program; `arsenal plan` prints a legal optimal squad
- [ ] **M3** Bottom-up expected points, backtested against completed gameweeks
- [ ] **M4** Source adapters and the agent research fan-out
- [ ] **M5** Session management and the three-layer executor
- [ ] **M6** GitHub Actions scheduling, Telegram, full autonomy

M1–M3 give a system that offers good advice with zero auth risk. M5–M6 add hands.
The breakable part is built last, on top of something already useful.

## Licence

MIT

# ai.rsenal — autonomous FPL manager

**Status:** draft v1 · **Date:** 2026-09-19 · **Season:** 2026/27 (GW5 current)

## 1. Problem

I forget to manage my FPL team. Deadlines pass with an injured captain, a
suspended defender in the XI, and free transfers rotting in the bank. The cost
is not that my decisions are bad — it is that I do not make them at all.

**Goal:** a system that autonomously manages the team to a competent standard,
never misses a deadline, and tells me what it did and why.

The bar is *reliable competence*, not brilliance. Never missing a deadline, never
fielding a flagged player, and always having a sensible captain beats occasional
inspiration. An agent that plays the percentages every single week will finish
well above the median, and that is the win condition.

## 2. Scope

**In:** transfers (including hits), starting XI, captain and vice-captain, bench
order, all four chips, multi-gameweek planning, research across match data,
advanced stats, creator content and community sources, post-deadline reporting.

**Out (v1):** mini-league-aware rank-chasing strategy, head-to-head leagues,
cup, live in-play alerts, a web dashboard, managing multiple teams.

**Non-goals:** beating the top 1k overall. Optimising for that means variance
and differentials; this system optimises for expected points and consistency.

## 3. Hard constraints

### 3.1 There is no write API

This is the defining constraint and it shapes the whole design.

- `users.premierleague.com` — the login host every FPL library and tutorial
  still documents — **no longer resolves**. Code written against it fails with
  DNS errors.
- Auth moved to `account.premierleague.com`, a bot-protected SSO returning
  **403 to any non-browser client**. There is no scriptable credential flow.
- `/api/my-team/{id}/` returns **403** unauthenticated.
- Reads are fully open and unusually rich.

**Consequence:** authenticated state must be harvested from a real browser and
replayed. Sessions expire — assume weeks — and are partly fingerprint-bound, so a
session minted on a desktop and replayed from a GitHub Actions runner in another
country is **the single most likely failure point in the system.**

The design treats session expiry as a *normal operating condition with a defined
recovery path*, never as an exception. See §6.6.

### 3.2 Everything else

- FPL API is undocumented, unversioned, and changes shape between seasons.
  Parse defensively, validate at the boundary, fail loudly.
- It is a free service for players, not an API product. Cache aggressively,
  rate-limit, identify the client honestly.
- Scraped sources (Understat, FBref) need caching, rate limits, and `robots.txt`
  compliance.
- GitHub Actions gives 2000 free minutes/month. A full deadline pipeline must
  cost single-digit minutes.

## 4. Architecture

### 4.1 The central commitment

> **The LLM supplies judgment. The solver supplies correctness.**

- **LLM** decides *what a player is worth* — reading press conferences, weighing
  conflicting injury reports, assessing rotation risk. Judgment under uncertainty
  over unstructured evidence. A solver cannot do this at all.
- **Integer program** decides *which players to own* — budget, squad size,
  position counts, 3-per-club, formation legality, transfer costs, jointly over a
  multi-gameweek horizon. Combinatorial optimisation under hard constraints. An
  LLM cannot do this reliably; it produces confidently illegal squads.

Blurring this is the main way systems like this fail. An LLM "sanity-checking"
the solver's output re-introduces exactly the errors the solver exists to
eliminate. When the model disagrees with the solver, the fix is to change the
**xP inputs** and re-solve — a principled, traceable edit — never to override the
output.

### 4.2 Pipeline

A staged, resumable DAG. Each stage writes a typed artifact to
`data/runs/{gw}/{stage}.json`, so a failed run resumes from the last good stage
and every decision is reproducible from its inputs.

```
                      ┌─────────────────────────────────┐
                      │  orchestrator (T-minus aware)   │
                      └────────────────┬────────────────┘
                                       │
   ┌───────────┐   ┌──────────┐   ┌────▼─────┐   ┌──────────┐   ┌──────────┐
   │ snapshot  │──▶│ research │──▶│ forecast │──▶│ optimise │──▶│ validate │
   └───────────┘   └────┬─────┘   └──────────┘   └──────────┘   └────┬─────┘
    FPL API,            │          xP + σ per      PuLP/CBC          │
    fixtures,           │          player/GW       integer program    │
    my-team             │          (deterministic) (no LLM)          │
                        │                                            │
        ┌───────────────┼───────────────┬──────────────┐             │
        ▼               ▼               ▼              ▼             ▼
   news-scout    stats-analyst   fixture-analyst  community    ┌──────────┐
   (T1/T3)          (T2)             (T1/T2)     -scout (T4)   │ execute  │
   availability   underlying       difficulty,   hypotheses,   └────┬─────┘
   minutes        xG/xA/DC         blanks,       ownership     1 API
   pressers       regression       doubles                     2 browser
                        │                                      3 advisory
                        └──▶ Evidence[] (tiered, sourced, timestamped)
                                                                    │
                                                              ┌─────▼────┐
                                                              │  notify  │
                                                              └──────────┘
                                                               Telegram
```

### 4.3 Components

| Module | Responsibility |
|---|---|
| `fpl/client.py` | Read API, caching, retry, rate limiting |
| `fpl/auth.py` | Session load, validation, expiry detection |
| `fpl/schemas.py` | Pydantic models — the defensive parse boundary |
| `fpl/rules.py` | Rules engine + **independent** squad validator |
| `fpl/writer.py` | Three-layer executor (API → browser → advisory) |
| `sources/*` | One adapter per source, all → `list[Evidence]` |
| `agents/*` | LLM orchestration; research fan-out, claim extraction |
| `forecast/*` | Evidence + history → xP with uncertainty |
| `optimizer/*` | The integer program |
| `notify/telegram.py` | Post-deadline reporting |
| `cli.py` | `arsenal run|plan|execute|doctor|rules|notify` |

## 5. Data model

```python
Evidence  # one sourced claim: tier, url, published_at, confidence, impact
PlayerForecast  # per player per GW: xP, sigma, p_plays, p_60, components
SquadState  # 15 picks with purchase/selling price, bank, FT, chips left
TransferPlan  # ordered in/out, hit cost, reasoning, evidence refs
GameweekPlan  # TransferPlan + XI + captain + vice + bench + chip + xP
RunRecord  # stage artifacts, timings, failures, degradations
```

All money in **integer tenths of a million**. `now_cost = 75` is £7.5m. Floats
eventually produce an off-by-0.1 squad the API rejects at the deadline.

## 6. Behaviour

### 6.1 Schedule

Relative to `deadline_time`, never a fixed cron — deadlines move for midweek
and holiday rounds. Actions wakes hourly and no-ops outside a window.

| Window | Stages |
|---|---|
| T−72h | snapshot, research (full fan-out) |
| T−24h | forecast, optimise → **provisional plan notified** |
| T−3h | research (news only), re-forecast, re-optimise |
| T−90m | validate, execute |
| T−30m | read back and verify |
| T+2h | notify (final summary) |

Submitting at T−90m rather than T−5m trades a little late information for a
recovery window. The T−24h provisional notification is never skipped, even in
full-auto: it is the only cheap chance to catch a broken forecast before it costs
points.

### 6.2 Autonomy

Full auto. The agent submits without approval, then reports. Configured bounds
in `config.yaml` rather than hardcoded:

- `max_hit` — most points to spend on transfers (default 4, i.e. one hit)
- `chip_autonomy` — whether chips fire automatically (default: yes, except
  Wildcard, which is escalated for approval given it rewrites the whole squad)
- `min_confidence` — forecast confidence floor below which the agent holds

Exceeding a bound degrades to advisory + notification rather than aborting
silently.

### 6.3 Optimisation

Rolling 5-gameweek horizon, discounted at γ≈0.85, maximising xP net of −4 hits,
risk-adjusted as `xP − κσ` to stop the solver chasing the upper tail. Chip
*timing* is a separate longer-horizon fixture-density scan that hands the main LP
a recommended window — a 5-week horizon cannot see a Bench Boost double ten weeks
out. Full formulation in the `fpl-optimiser` skill.

### 6.4 Validation

Every invariant in the `fpl-rules` skill is re-checked by a validator written
**independently of** the optimiser's constraint code. Shared code means shared
bugs, invisible precisely when they matter. Validation re-reads `my-team/` fresh
and checks against the server's current truth, not the T−72h snapshot.

Any failure aborts before submission. A missed gameweek costs a few points; a
wrong submission costs more and is irreversible.

### 6.5 Execution

Three layers, in order, falling through on failure:

1. **Direct API** with replayed cookies — fast, cheap
2. **Playwright browser replay** with stored `storage_state` — slower, survives
   some checks the direct call fails
3. **Advisory** — no write, full recommendation, `needs_manual_action`

**Transfers first, then picks** — captain and bench reference players you may not
own until the transfer lands. After any write, read back and verify; a 200 is not
proof.

### 6.6 Session lifecycle

The expected failure, designed for rather than handled:

1. Seed locally: `arsenal auth login` opens a real browser, you log in once, it
   saves `storage_state.json`.
2. `arsenal auth export` emits the secret value for `FPL_SESSION_JSON`.
3. Every run validates the session before acting.
4. On expiry: fall through the execute layers; if all fail, emit advisory,
   notify with explicit re-seed instructions, and **keep producing
   recommendations every gameweek until fixed**. The agent stays useful degraded.
5. Each successful browser run refreshes and re-persists `storage_state`,
   extending life without intervention.

### 6.7 Reporting

Telegram after every deadline: transfers with reasoning and the evidence that
drove them, captain and why, chip decision *including why a chip was considered
and rejected*, formation, hit taken and its justification, horizon xP, and
anything that failed or degraded.

**Failures are reported prominently.** The summary is the only window into an
otherwise invisible system; one that hides degradation is worse than none.

## 7. Deployment

GitHub Actions, hourly cron, `uv` for dependencies, ~2–5 min per active run.

Secrets: `FPL_TEAM_ID`, `FPL_SESSION_JSON`, `ANTHROPIC_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `YOUTUBE_API_KEY`,
`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`.

Run artifacts upload per run; `data/runs/` is committed back for an audit trail.
The repo becomes the decision log.

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Session expires mid-season | **High** | Three-layer degradation; advisory keeps working; explicit re-seed flow |
| Fingerprint binding blocks CI writes | **High** | Browser layer; if persistently blocked, move execute to a local runner and keep research in CI |
| API shape change | Medium | Defensive parse, raw payload persistence, `doctor` drift check |
| Bad forecast → bad transfer | Medium | Risk adjustment, `max_hit` bound, T−24h provisional notification |
| Scraper breakage | Low | Adapters degrade to empty; abort only past half-failed |
| LLM hallucinated player/stat | Medium | Every claim needs a resolving URL; ids resolved to `element_id`; solver only sees numbers |
| Cost overrun (LLM tokens) | Low | Research scoped to ~75 players; cached by input hash |

## 9. Milestones

**M1 — Foundation.** Read client, schemas, rules engine, validator, config, CLI
skeleton, `doctor`. Tests on the rules engine, especially sell-on fee.

**M2 — Optimiser.** Integer program with all constraints, hand-built fixtures
with known optima, naive xP baseline. `arsenal plan` prints a legal squad.

**M3 — Forecast.** Bottom-up xP from FPL API data alone. Backtest against
completed gameweeks.

**M4 — Research.** Source adapters and the agent fan-out. Evidence flows into xP.

**M5 — Execution.** Session management, three-layer writer, dry-run verified
against a real browser request.

**M6 — Autonomy.** Actions workflow, Telegram, T-minus scheduling. Full auto.

M1–M3 deliver a system that gives good advice with zero auth risk. M5–M6 add
hands. That ordering means the risky, breakable part is built last, on top of
something already useful.

## 10. Open questions

- **Are the write payload shapes correct?** Reconstructed from browser network
  calls, not independently verified. Must be confirmed by diffing `arsenal
  execute --dry-run` against a real browser submission before the first live run.
- **Are the DC thresholds still 10/12?** Confirmed as 2 points and GKP-excluded
  from `game_config`; the thresholds are engine-side and not exposed. Validate
  empirically against observed awards.
- **Will a CI-replayed session hold?** Unknown until tested across several
  gameweeks. Determines whether execution can stay in Actions.
- Backtest methodology and what xP accuracy is good enough to trust unattended.

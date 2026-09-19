---
name: fpl-deadline-run
description: Operational runbook for a gameweek decision — the T-minus schedule, the ordered pipeline from data pull through research, forecast, optimisation, validation, submission and notification, plus abort conditions and recovery. Load when running, debugging, or modifying the deadline pipeline.
---

# Deadline runbook

One gameweek decision, start to finish. The pipeline is a **staged, resumable
DAG**: every stage writes a typed artifact to `data/runs/{gw}/{stage}.json`, so a
failed run resumes from the last good stage instead of restarting, and every
decision is reproducible after the fact from its inputs.

```
snapshot → research → forecast → optimise → validate → execute → notify
```

## T-minus schedule

Scheduled relative to `events[].deadline_time`, never a fixed cron, because
deadlines move for midweek and holiday rounds. The GitHub Actions workflow wakes
hourly and does nothing unless it is inside a window.

| Window | Stage | Purpose |
|---|---|---|
| **T−72h** | `snapshot`, `research` | Full data pull, deep research fan-out. Expensive work done early, while there is time to fix a failure. |
| **T−24h** | `forecast`, `optimise` | Build xP, solve. Produce a *provisional* plan and notify it — this is the last comfortable point for a human to object. |
| **T−3h** | `research` (news only), re-`forecast`, re-`optimise` | Catch late pressers and confirmed injuries. Almost all decision-changing information arrives here. |
| **T−90m** | `validate`, `execute` | Re-read `my-team/`, revalidate, submit. |
| **T−30m** | verify | Read back picks and confirm the server agrees with intent. |
| **T+2h** | `notify` | Post-deadline summary: what changed, why, what was rejected. |

Two deliberate choices:

- **Submit at T−90m, not T−5m.** The marginal information in the last hour is
  small; the risk of a failed run with no time to recover is not. Late news after
  submission can still be acted on by re-running — transfers remain editable
  until the deadline.
- **Never skip the T−24h provisional notification**, even in full-auto. It is
  the only cheap opportunity to catch a broken forecast before it costs points.

## Stages

### 1. `snapshot`

Pull and persist raw payloads: `bootstrap-static`, `fixtures`, `event-status`,
`set-piece-notes`, `element-summary` for owned and shortlisted players, and
authenticated `my-team/{id}/`.

Write raw bytes to `data/runs/{gw}/raw/` **before parsing**. When a payload shape
changes mid-season, the bytes that broke it are irreproducible after the fact.

Establish here: current GW, deadline (UTC), free transfers available, bank,
squad value, **purchase price of every owned player**, and chips still unspent.
`my-team/` is the only source for purchase prices, so if it 403s, the run cannot
compute a legal budget — go straight to the degradation path.

### 2. `research`

Fan out across source adapters concurrently. Each returns `list[Evidence]` and
**degrades to empty rather than raising** — one dead scraper must not cost a
gameweek.

Scope the fan-out: the full 660-player universe is wasteful. Research your 15
owned players, plus a candidate shortlist (~60) filtered by price reachability,
form, fixtures and ownership momentum. Always research owned players even if
they look settled — that is exactly where a surprise injury hurts most.

See `fpl-research` for tiering and extraction discipline.

### 3. `forecast`

Turn evidence + history into per-player, per-gameweek xP with uncertainty, across
the planning horizon (default 5 gameweeks). Deterministic given its inputs;
cache keyed on an input hash so re-running without new evidence is free.

Output `PlayerForecast` for every player in the shortlist ∪ owned squad. Missing
players must be explicitly absent, never silently zero — zero is a decision, and
an accidental zero benches a good player.

### 4. `optimise`

The integer program. See `fpl-optimiser`. Consumes xP + current squad + rules,
emits an ordered transfer plan, starting XI, captain, vice-captain, bench order,
and a chip recommendation. **This stage does no LLM inference at all** — it is
pure arithmetic, and it is the only stage permitted to decide what the squad is.

### 5. `validate`

Independent re-derivation of every invariant in `fpl-rules`, deliberately *not*
reusing the optimiser's own constraint code — a bug shared between solver and
validator is invisible. Re-read `my-team/` fresh and check the plan against the
server's current truth, not the T−72h snapshot.

Explicitly re-check: deadline has not passed, prices have not moved, free
transfer count matches, no newly flagged player, and the exact hit cost.

Any failure aborts before submission. **A missed gameweek costs a few points; an
illegal or wrong submission costs more and is irreversible.**

### 6. `execute`

Three layers, tried in order, each falling through on failure:

1. **Direct API** — `POST /api/transfers/` then `POST /api/my-team/{id}/` with
   replayed session cookies. Fast and cheap.
2. **Browser replay** — Playwright with stored `storage_state`, driving the real
   UI. Slower, survives some anti-bot checks the direct call fails.
3. **Advisory** — no write. Emit the full recommendation and flag
   `needs_manual_action`.

Ordering is load-bearing: **transfers first, then picks.** Captain and bench
order reference players you may not own until the transfer lands, so the reverse
order fails on a squad you just changed.

`--dry-run` prints exact payloads without sending. Use it to diff against a real
browser request before the first live submission.

After any write, **read back and verify the server's state matches intent.** A
200 response is not proof; confirm the picks.

### 7. `notify`

Telegram summary containing: transfers in/out with reasoning and the evidence
that drove them, captain and why, chip decision (including *why not*, when a chip
was considered and rejected), formation and bench order, hit taken and its
justification, expected points across the horizon, and anything that failed or
degraded.

**Report honestly.** If a source was down, a session expired, or the run fell
back to advisory, say so prominently. The summary is the only window into an
otherwise invisible system, and a summary that hides failure is worse than none.

## Abort conditions

Stop before submission, notify, and leave the team untouched:

- Deadline already passed, or under 10 minutes remaining at validation.
- `my-team/` unreadable — no purchase prices means no legal budget.
- Validation fails any `fpl-rules` invariant.
- Optimiser infeasible (usually a bad budget or a stale price).
- Forecast covers fewer than 11 of the current squad — evidence pipeline is broken.
- Proposed hit exceeds the configured `max_hit` without an explicit override.
- More than half the source adapters failed.

An aborted run leaves the existing team in place. **The default action is always
"do nothing"**, which for a team already picked is a perfectly acceptable
outcome — and is why the team should never be left in an invalid state between
runs.

## Degradation and recovery

**Session expired (403).** The expected failure, not an exceptional one.
Fall through the execute layers; if all fail, emit advisory output, notify with
an explicit "your session expired, re-seed it" message and a link to the refresh
instructions, and keep producing recommendations every gameweek until fixed. The
agent stays useful while degraded.

**Source failures.** Proceed on what succeeded; record which sources were missing
and widen uncertainty accordingly. Abort only past the half-failed threshold.

**Optimiser infeasible.** Relax in a fixed order — drop the chip, then reduce max
transfers to 1, then to 0 (picks-only). A picks-only run that fixes the captain
and bench is still most of the value.

**Partial submission** (transfers landed, picks failed). The dangerous case: the
squad changed but XI/captain did not. Retry picks immediately, and if it still
fails, notify **urgently** — an unset captain is a guaranteed points loss, and
this is the one failure mode worth waking someone up for.

## Idempotency

Runs must be safely repeatable — the workflow may fire twice, or be retried.

- Key every run on `(gw, stage, input_hash)`; skip stages whose inputs are unchanged.
- Before transferring, diff intent against the **server's** current squad. If the
  transfer already landed, skip it rather than re-submitting.
- Never derive "did I already act?" from local state alone; local state can be
  lost between runner invocations. The server is the truth.

---
name: fpl-api
description: Reference for the undocumented Fantasy Premier League HTTP API — read endpoints and their payload shapes, the post-2025 authentication model, the write path for transfers/picks/chips, and request etiquette. Load before writing any code that talks to fantasy.premierleague.com, and before debugging a 403.
---

# FPL HTTP API

Base: `https://fantasy.premierleague.com/api/`

There is no official documentation, no versioning, and no stability guarantee.
Fields appear and vanish between seasons. **Parse defensively** — treat every
field as optional and validate into typed models at the boundary
(`src/arsenal/fpl/schemas.py`) so a shape change fails loudly in one place
instead of producing a subtly wrong squad three layers down.

All endpoints below were verified live. Re-verify with `uv run arsenal doctor`,
which probes each one and reports drift.

## Read endpoints (no auth)

| Endpoint | Contents |
|---|---|
| `bootstrap-static/` | **The main payload.** All players (`elements`), `teams`, `events` (gameweeks), `element_types`, `game_config` (scoring + rules), `chips`. ~660 players, several MB. |
| `fixtures/` | Every fixture. `?event={gw}` filters to one gameweek; `?future=1` for unplayed. |
| `element-summary/{player_id}/` | Per-player match-by-match `history`, `history_past` (prior seasons), and upcoming `fixtures` with difficulty. |
| `event/{gw}/live/` | Live per-player stats and points for a gameweek. Large (~470KB). The source of truth for what actually happened. |
| `entry/{team_id}/` | Public manager profile: name, overall rank, team value, bank, chip usage summary. |
| `entry/{team_id}/history/` | Season-by-season history, per-gameweek results, and **`chips` — the authoritative record of which chips are already spent.** |
| `entry/{team_id}/event/{gw}/picks/` | A manager's picks for a *completed or current* gameweek. Public, but only after the deadline. |
| `event-status/` | Whether bonus and league tables have been processed for the current GW. Poll this to know when results are final. |
| `dream-team/{gw}/` | Highest-scoring XI of the gameweek. |
| `team/set-piece-notes/` | **Per-club set-piece taker notes.** Editorially maintained, high signal for penalty and dead-ball assignment. Underused. |
| `leagues-classic/{id}/standings/` | Classic league standings, paginated via `?page_standings=`. League `314` is the global Overall league. |

### Fields worth knowing on `elements`

- **Availability:** `status` (`a` available, `d` doubtful, `i` injured, `s`
  suspended, `n` on loan/ineligible, `u` unavailable/left),
  `chance_of_playing_next_round` (0–100 or null), `news`, `news_added`.
  `can_transact` / `can_select` are hard gates — respect them.
- **Underlying:** `expected_goals`, `expected_assists`,
  `expected_goal_involvements`, `expected_goals_conceded`, and `_per_90`
  variants. These are Opta numbers supplied to FPL — they let you skip Understat
  scraping for most purposes.
- **Defensive:** `defensive_contribution`, `defensive_contribution_per_90`,
  `clearances_blocks_interceptions`, `recoveries`, `tackles`.
- **Set pieces:** `penalties_order`, `direct_freekicks_order`,
  `corners_and_indirect_freekicks_order` (1 = first choice, null = not a taker).
  A `penalties_order = 1` forward is worth materially more than his xG suggests.
- **Price:** `now_cost` (tenths), `cost_change_event`, `cost_change_start`,
  `price_change_projections`, `price_change_hourly_rate`,
  `price_change_locked_until`.
- **Market:** `selected_by_percent`, `transfers_in_event`,
  `transfers_out_event`, `form`, `ep_this`, `ep_next` (FPL's own expected points
  — a useful baseline to beat, not a target to copy).
- **Editorial:** `scout_risks`, `scout_news_link`.

`fixtures` carry `team_h_difficulty` / `team_a_difficulty` (FDR, 1–5). FDR is a
coarse pre-season editorial rating. **Prefer a difficulty measure you compute
yourself** from opponent xG-for/xG-against form; fall back to FDR only as a prior
in the first few gameweeks when the sample is thin.

## Authentication (changed — most guides are wrong)

**`users.premierleague.com` no longer resolves.** Every tutorial, and every
release of the `fpl` Python library, posts credentials to
`https://users.premierleague.com/accounts/login/`. That host is dead (NXDOMAIN).
Code written against it fails with a DNS error, not an auth error, which is a
confusing way to discover this.

Identity now lives at `account.premierleague.com`, which is a bot-protected SSO
that returns **403 to any non-browser client**. There is no username/password
flow you can drive from `requests` or `httpx`.

### What this means practically

Authenticated state must be **harvested from a real browser** and replayed:

1. Log in once at `https://fantasy.premierleague.com/` in a real browser.
2. Export the session cookies — `pl_profile` and `sessionid` are the load-bearing
   ones — or capture a full Playwright `storage_state.json`.
3. Store it as a secret (`FPL_SESSION_JSON` in GitHub Secrets) and replay it.

Sessions expire. Assume weeks, not months, and treat expiry as a *normal*
operating condition with a defined recovery path, never an exception. Cookies are
also partly bound to client fingerprint, so a session minted on your desktop and
replayed from a GitHub Actions runner in a different country is the most likely
single point of failure in this whole system. Design for it to break.

### Authenticated endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `my-team/{team_id}/` | GET | **Your current squad including `purchase_price` and `selling_price` per pick, plus `transfers.limit` / `.made` / `.bank` / `.value`.** The only source of purchase prices — you cannot compute a legal budget without it. |
| `transfers/` | POST | Submit transfers. |
| `my-team/{team_id}/` | POST | Set starting XI, bench order, captain, vice-captain, and activate a chip. |

Returns **403** when unauthenticated — which is also what it returns for an
*expired* session, so 403 alone does not tell you which. Distinguish them by
probing `my-team/` with the session before acting.

### Write payloads

Both writes need these headers:

```
Content-Type: application/json
X-Requested-With: XMLHttpRequest
Referer: https://fantasy.premierleague.com/my-team
Origin:  https://fantasy.premierleague.com
```

`POST /api/transfers/`:

```jsonc
{
  "entry": 1234567,
  "event": 6,
  "transfers": [
    { "element_in": 427, "purchase_price": 75,
      "element_out": 182, "selling_price": 72 }
  ],
  "chip": null,     // or "wildcard" | "freehit"
  "confirmed": true
}
```

Prices must match what the server currently believes, to the tenth. If a price
moved between your read and your write, the server rejects the whole payload.
**Always re-read `my-team/` immediately before writing** and build the payload
from that response, never from cached bootstrap data.

`POST /api/my-team/{team_id}/`:

```jsonc
{
  "picks": [
    { "element": 427, "position": 1, "is_captain": false, "is_vice_captain": false }
    // ... 15 entries; positions 1-11 start, 12-15 bench in order, 15 = reserve GK
  ],
  "chip": null      // or "bboost" | "3xc"
}
```

> The read endpoints and the auth model above are verified. **The exact write
> payload shapes are reconstructed from the browser's own network calls and are
> not independently verified here.** Before the first live submission, confirm
> them by performing one transfer manually in a browser with DevTools open and
> diffing the real request against what `arsenal` would send. `arsenal execute
> --dry-run` prints the exact payload for this comparison. Do not skip this.

## Etiquette and reliability

There is no published rate limit, but this is a free service run for players,
not an API product. Behave accordingly:

- **Cache aggressively.** `bootstrap-static` changes meaningfully once a day
  (prices at ~01:30 UTC) plus during live matches. Cache to
  `data/cache/` with a TTL and honour it. A full pipeline run should hit the
  network a handful of times, not hundreds.
- **One request at a time** to a given endpoint family; no parallel hammering.
- Exponential backoff on 429/5xx, and a real `User-Agent` identifying the
  project and a contact address.
- Never poll faster than once a minute, even during live gameweeks.
- Persist raw payloads before parsing. When a shape changes mid-season you want
  the bytes that broke it, and they are irreproducible after the fact.

## Failure modes to handle explicitly

| Symptom | Cause | Response |
|---|---|---|
| DNS error on `users.premierleague.com` | Following a stale guide | Use the browser-session model above |
| 403 on `my-team/` | Session expired or fingerprint-bound | Escalate to session refresh; notify; degrade to advisory |
| Transfer rejected, prices differ | Price moved between read and write | Re-read `my-team/`, rebuild payload, retry once |
| `bootstrap-static` missing a field | Season shape change | Fail loudly at the schema boundary; do not default silently |
| Deadline passed mid-run | Slow pipeline | Check `deadline_time` again immediately before writing; abort if passed |

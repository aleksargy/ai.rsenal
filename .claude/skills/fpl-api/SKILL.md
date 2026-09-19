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

## Authentication — bearer tokens, not cookies

**Every FPL guide, tutorial and library is wrong about this.** Verified
empirically against a live logged-in account, 2026/27 season:

| | What is documented everywhere | What actually works |
|---|---|---|
| Login host | `users.premierleague.com/accounts/login/` | **NXDOMAIN** — the host no longer resolves |
| Credential | `pl_profile` + `sessionid` cookies | **Absent.** `ST` / `ST-NO-SS` appear instead |
| Replaying cookies | Authenticates | **403** |
| `Authorization: Bearer <access_token>` | Not mentioned anywhere | **Authenticates** |

FPL now authenticates through **`account.premierleague.com`**, an OpenID Connect
provider (a PingOne tenant). The browser's OIDC client stores its tokens in
`localStorage` under a key shaped:

```
oidc.user:https://account.premierleague.com/as:<client_id>
```

The value is a **JSON wrapper** — `{"id_token": ..., "access_token": ...,
"refresh_token": ..., "expires_at": ...}` — not a bare JWT, so a "does it start
with `eyJ`" scan finds nothing. Parse it.

Use the **access token**, never the id token. An id token asserts who the user
is to the client; an access token authorises API calls. Substituting one gives a
credential that looks plausible and always 403s.

### The constraint that shapes everything: one-hour tokens

Access tokens carry `exp - iat = 3600`. **One hour.** A deadline run happens days
after capture, so a stored access token is always dead on arrival. The refresh
token is the durable credential.

Discovery document (`/as/.well-known/openid-configuration`) confirms the grant:

```
token_endpoint:        https://account.premierleague.com/as/token
grant_types_supported: [..., refresh_token, ...]
```

Refreshing needs no browser and no client secret — the client is a public SPA
using PKCE:

```http
POST https://account.premierleague.com/as/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<token>&client_id=<client_id>
```

The `client_id` is the segment after the final colon of the localStorage key, and
also appears as a `client_id` claim inside the access token.

**Write back a rotated refresh token.** Providers may issue a new one on each
refresh; keeping the old value when none is returned is correct, but discarding a
new one destroys the only durable credential you have.

This is *better* than the cookie model it replaced. A refresh token is designed
to be replayed from a server, where a session cookie bound to browser
fingerprint was always going to be fragile from CI. The spec's original worry
about sessions breaking when replayed from a GitHub Actions IP largely goes away.

### Capturing a session

There is no scriptable login — `account.premierleague.com` returns 403 to
non-browser clients, and Google SSO blocks automation-controlled browsers
outright ("This browser or app may not be secure"). So:

1. Start a Chromium-based browser with `--remote-debugging-port=9222`
   (close all its windows first, or the flag is silently ignored).
2. Log into FPL and open **My Team**.
3. `arsenal auth attach` reads cookies *and* localStorage over CDP.

localStorage is only readable from an **open page on the origin**, so the FPL tab
must be open when attaching.

**Never gate on a cookie name.** The names changed this season with no
announcement, and a check for the old ones rejected a working session. Capture
everything, and let `my-team/` be the only authority on what authenticates.

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

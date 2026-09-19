---
name: fpl-research
description: How to gather, weight, and record FPL intelligence from match data, advanced stats, YouTube creator transcripts, blogs, Reddit and press conferences — and how to turn it into a structured expected-points forecast the optimiser can consume. Load before any research fan-out, before writing a source adapter, or when deciding how much to trust a claim.
---

# FPL research

The optimiser is only as good as the expected-points (xP) vector you hand it.
This skill is about producing that vector honestly.

The governing principle: **the LLM's job is judgment on unstructured
information, not arithmetic.** Read prose, watch for team news, weigh
conflicting claims, assess rotation risk — then emit numbers with explicit
uncertainty. Never let the model compute a squad, a budget, or a points total;
that is the solver's job, and the solver is provably correct where the model is
merely plausible.

## Source hierarchy

Weight claims by how close they are to ground truth. When sources conflict,
**higher tier wins outright** — do not average a manager's press conference
against a YouTuber's speculation.

**Tier 1 — Fact.** Official FPL API, completed match data, confirmed lineups,
official club injury statements. Deterministic, machine-readable, no
interpretation needed. `status`, `chance_of_playing_next_round` and `news` on an
element are club-sourced and authoritative.

**Tier 2 — Measured.** Understat / FBref / Opta underlying numbers: npxG, xA,
shot volume and location, minutes trends, set-piece share, defensive action
counts. Objective, but requires interpretation — a high xG on three shots is
noise, on thirty it is signal.

**Tier 3 — Reported.** Press conference quotes, beat reporters, Fantasy Football
Scout team-news pages. Genuinely informative about intent — a manager saying "he
trained today" is real evidence — but managers mislead, and reporters
paraphrase. Attribute to a named source and date, always.

**Tier 4 — Opinion.** YouTube creators, Reddit threads, blog predictions,
template chatter. Useful for two things and two things only: (a) surfacing
information you would otherwise have missed — an injury rumour, a tactical shift
— which you then **verify at a higher tier before acting**, and (b) gauging what
the field is doing, which matters for differential strategy. **Never let Tier 4
move an xP number on its own.**

The failure mode to guard against: a confident YouTuber asserting a player is
"nailed on to start" the week before that player is benched. Popularity is not
accuracy. Treat creator claims as *hypotheses to check*, never as evidence.

## Evidence records

Every claim entering the pipeline becomes an `Evidence` record. Untraceable
claims are discarded, not downweighted.

```python
class Evidence(BaseModel):
    player_id: int | None  # None for team/fixture-level claims
    team_id: int | None
    claim: str  # one factual assertion, not a paragraph
    tier: Literal[1, 2, 3, 4]
    source_url: str
    source_name: str
    published_at: datetime  # UTC; recency gates relevance
    confidence: float  # 0-1, the extractor's own certainty
    impact: Literal["availability", "minutes", "role", "set_pieces", "form", "fixture", "price"]
    retrieved_at: datetime
```

Rules:

- **One claim per record.** "Saka is fit and will take penalties" is two claims
  with different verification paths and different impacts.
- **`source_url` is mandatory and must resolve.** If you cannot cite it, you
  cannot use it. A model-generated recollection is not evidence.
- **Never paraphrase a quote into a fact.** "Arteta said Saka *should* be
  available" ≠ "Saka is available". Preserve the hedging; hedging is data.
- **Stale evidence expires.** Availability claims decay fast — a Tuesday injury
  report is superseded by Friday's presser. Weight by `published_at` relative to
  the deadline, and discard availability claims older than ~10 days outright.

## Source adapters

Each lives in `src/arsenal/sources/` behind a common interface, returns
`list[Evidence]`, caches to `data/cache/`, and **degrades to empty rather than
raising**. One dead source must never take down a deadline run — a partial
forecast beats no submission.

### FPL API + fixtures (Tier 1)

Free, structured, no scraping. Covers availability, prices, ownership, set-piece
order, and Opta underlying stats. **Start here for every question** — a
surprising amount of what people scrape for is already in `bootstrap-static`.

Compute your own fixture difficulty rather than trusting FDR: rate each
opponent by rolling xG-for and xG-against over a recent window, adjust for home
advantage, and build a forward-looking schedule score across the planning
horizon. Explicitly handle blanks (no fixture) and doubles (two fixtures) — both
are decisive for chip timing and neither is visible in a naive per-gameweek join.

### Understat / FBref (Tier 2)

Shot-level and possession-adjusted data richer than the FPL feed: npxG separates
penalty inflation from open play, and shot location distinguishes a poacher from
a speculative long-range shooter.

Both are scraped, so: identify yourself, cache hard (this data changes once per
match), rate-limit to seconds between requests, and respect `robots.txt`.
Understat embeds JSON in a `<script>` tag rather than serving an API. Player-name
matching between FBref/Understat and FPL ids is the classic failure — build an
explicit alias map, verify it by club and position, and **fail loudly on an
unmatched name** rather than silently dropping a player from the forecast.

### YouTube creators (Tier 4, occasionally 3)

Fetch transcripts via the YouTube Data API for a configured channel list, then
have an extraction agent pull structured claims.

- Transcripts are **noisy**: auto-captions mangle player names constantly.
  Resolve every mention to an FPL `element_id` and drop what you cannot resolve.
- Videos are **stale on arrival** — a Monday video is often obsolete by Friday's
  presser. Weight hard by publish date relative to the deadline.
- Creators are **incentivised toward engagement**, which rewards bold calls.
  Systematically discount confident assertions that lack a cited source.
- Extract *claims*, not vibes. "He's a great differential" is unusable. "He is
  expected to start against Spurs, per the manager's presser" is checkable.
- When a creator cites a primary source, **follow the citation and record the
  primary instead**, promoting it to its proper tier.

### Blogs, Reddit, press conferences (Tier 3–4)

r/FantasyPL scout threads and press-conference roundups are the fastest route to
late team news — often the single highest-value input in the final hours, since
it is the one thing statistics cannot supply.

Reddit specifically: use the official API with a registered app and a descriptive
`User-Agent`. Score comments by upvotes *and* whether they link a primary source;
an unsourced highly-upvoted comment is still Tier 4. Press-conference content
from a named journalist quoting a manager directly is Tier 3 and can move a
forecast on its own.

## From evidence to expected points

Build xP **bottom-up per player per gameweek**, never as a single vibes-based
number:

```
xP = P(plays) × [ xP_appearance
                + xP_attacking      (from npxG, xA, penalty share)
                + xP_defensive      (clean sheet probability, DC threshold prob)
                + xP_bonus          (from BPS-driving underlying stats)
                − xP_negative       (cards, goals conceded) ]
```

Notes that matter:

- **`P(plays)` dominates everything.** A 12-xP player at 50% to start is worth
  less than a 7-xP nailed starter. This is where Tier 3 evidence earns its keep,
  and it is the single biggest lever the LLM has on the final answer.
- Separate `P(appears)` from `P(60+ minutes)` — they have different point
  consequences and very different distributions for rotation risks.
- **Defensive contribution is a threshold, not a rate.** Model
  `2 × P(actions ≥ threshold)`. See `fpl-rules`.
- Clean sheet probability comes from opponent attacking strength and your team's
  defensive record — a Poisson model on expected goals conceded is adequate and
  far better than an FDR lookup.
- **Emit an uncertainty band, not just a point estimate.** The optimiser uses it
  to prefer robust squads over knife-edge ones, and it is what makes the
  difference between a model that looks good and one that survives contact with
  a rotation.
- **Regress to the mean, hard, early in the season.** Five gameweeks is a tiny
  sample. A player on a 3-goal hot streak from 0.8 npxG is going to stop.
  Shrink toward a position-and-price prior with a weight that decays as minutes
  accumulate.

## Discipline

These are the rules that keep an auto-submitting agent from doing something
stupid.

1. **Never invent a player, price, fixture, or statistic.** Every number must
   trace to a retrieved payload. If you need a figure you do not have, fetch it
   or declare it missing — do not estimate it into existence.
2. **Resolve every player mention to an FPL `element_id`** before it enters the
   pipeline. Names are ambiguous (multiple players share surnames; transfers
   move them between clubs). An unresolved name is dropped and logged, never
   guessed.
3. **Record disagreement rather than resolving it silently.** If two Tier 3
   sources conflict on a player's fitness, that *is* the finding — it should
   widen the uncertainty band and may itself be reason to avoid the player.
4. **Distinguish "no evidence" from "evidence of no".** A player with no injury
   news is probably fine; a player whose source you failed to fetch is unknown.
   These must not collapse into the same number.
5. **Timestamp everything in UTC** and always reason relative to the deadline,
   not to wall-clock time.
6. **Prefer the boring, well-evidenced move.** The agent's edge is consistency
   and never missing a deadline — not clever punts. A −4 hit on a Tier 4 rumour
   is the most expensive mistake available.

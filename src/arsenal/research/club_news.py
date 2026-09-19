"""Official club sources, straight from the FPL payload.

Two things sit in `bootstrap-static` that nobody uses, and both are better than
anything you could scrape:

* **`scout_risks`** — structured, gameweek-scoped unavailability. Currently only
  `loan_ineligible`: a loaned player who cannot face their parent club in one
  specific gameweek. Deterministic, week-specific, and invisible everywhere else.
  A model without it will happily captain a player who is contractually barred
  from the pitch.

* **`scout_news_link`** — a direct link to the **official club article** behind a
  player's flag. Typically the manager's pre-match press conference on the club's
  own site: `arsenal.com/news/arteta-issues-update-on-...`,
  `avfc.co.uk/news/.../prematch-team-news-for-spurs-showdown/`.

That second one is the highest-value team-news source available, and it is free,
structured, and needs no scraping of a third party. A club publishing its own
manager's words is as close to primary as team news gets — Tier 3, and firmly at
the top of it.

Links are shared per club: six Aston Villa players point at one pre-match piece,
so 66 flagged players resolve to ~45 unique articles. Fetch each once.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from ..fpl.schemas import Bootstrap
from .evidence import Evidence, Tier
from .resolver import PlayerResolver
from .sources import USER_AGENT, Document, Source, SourceResult

log = logging.getLogger(__name__)

# Club sites are ordinary marketing sites, not APIs. Be unhurried and identify
# honestly; there are only ~45 of these per gameweek.
REQUEST_TIMEOUT = 20.0
MAX_ARTICLE_CHARS = 8000

# Below this, the extraction is navigation chrome rather than an article — the
# signature of a JavaScript-rendered page that served no body HTML.
MIN_ARTICLE_CHARS = 400

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\n\s*\n+")


def html_to_text(markup: str) -> str:
    """Strip HTML to readable text.

    Deliberately crude — no parser dependency. The consumer is a language model
    extracting claims, and it copes with imperfect text far better than it copes
    with a missing article. Scripts and styles are removed first because their
    contents would otherwise dominate the output.
    """
    text = _SCRIPT_OR_STYLE.sub(" ", markup)
    text = _TAG.sub("\n", text)
    # `html.unescape` covers the whole entity set, numeric ones included. A
    # hand-rolled list missed `&#233;`, so Sangaré reached the extractor as
    # "Sangar&#233;" — which then fails to resolve and the claim is dropped.
    text = html.unescape(text).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    return _WHITESPACE.sub("\n\n", "\n".join(line for line in lines if line))


class ScoutRiskSource(Source):
    """Gameweek-scoped unavailability from ``scout_risks``.

    Tier 1: this is not a report about a player, it is a scheduling fact. It also
    demonstrates why ``Evidence`` carries a gameweek — "ineligible in GW27" must
    not bench the player in GW26.
    """

    name = "scout-risks"

    def __init__(self, bootstrap: Bootstrap, raw_elements: list[dict] | None = None) -> None:
        self.bootstrap = bootstrap
        # `scout_risks` is not on the typed Element model (it is a nested,
        # rarely-populated structure), so the raw payload is read when available.
        self.raw = raw_elements or []

    def gather(self, resolver: PlayerResolver, **context: object) -> SourceResult:
        now = datetime.now(UTC)
        evidence: list[Evidence] = []

        for raw in self.raw:
            risks = raw.get("scout_risks") or []
            if not risks:
                continue
            element_id = raw.get("id")
            if element_id is None:
                continue
            for risk in risks:
                note = str(risk.get("notes") or "").strip()
                gameweek = risk.get("gameweek")
                if not note:
                    continue
                evidence.append(
                    Evidence(
                        player_id=int(element_id),
                        team_id=raw.get("team"),
                        claim=f"Unavailable: {note}",
                        tier=Tier.FACT,
                        impact="availability",
                        source_url=risk.get("url") or f"fpl://element/{element_id}/risks",
                        source_name="FPL scout risk",
                        published_at=now,
                        confidence=1.0,
                        gameweek=int(gameweek) if isinstance(gameweek, int) else None,
                    )
                )

        return SourceResult(name=self.name, evidence=evidence)


@dataclass
class ClubArticle:
    """One official club article, with the players FPL attached it to."""

    url: str
    club: str
    player_ids: list[int]


def club_article_links(raw_elements: list[dict], teams: dict[int, str]) -> list[ClubArticle]:
    """Group ``scout_news_link`` values into unique articles.

    Links are shared across a club's flagged players, so grouping turns 66 player
    flags into ~45 fetches. Query strings are stripped because FPL appends UTM
    tracking that differs per player and would otherwise defeat the grouping.
    """
    grouped: dict[str, ClubArticle] = {}
    for raw in raw_elements:
        link = raw.get("scout_news_link")
        element_id = raw.get("id")
        if not link or element_id is None:
            continue
        canonical = str(link).split("?")[0]
        article = grouped.get(canonical)
        if article is None:
            article = ClubArticle(
                url=canonical,
                club=teams.get(raw.get("team", 0), "?"),
                player_ids=[],
            )
            grouped[canonical] = article
        article.player_ids.append(int(element_id))
    return list(grouped.values())


def fetch_club_articles(
    articles: list[ClubArticle], *, limit: int = 50
) -> tuple[list[Document], list[str]]:
    """Fetch official club articles as documents for claim extraction.

    Returns ``(documents, problems)`` and never raises — a club site being down
    should cost that club's news, not the whole research run.

    These are marked **Tier 3**, not Tier 4. A club publishing its own manager's
    press conference is a primary source; the extractor's promotion rule then has
    nothing to promote, because it is already there.
    """
    documents: list[Document] = []
    problems: list[str] = []
    now = datetime.now(UTC)

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    with httpx.Client(
        headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        for article in articles[:limit]:
            try:
                response = client.get(article.url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                problems.append(f"{article.url}: {exc}")
                log.info("club article fetch failed: %s", exc)
                continue

            text = html_to_text(response.text)
            if len(text) < MIN_ARTICLE_CHARS:
                # Almost always a JavaScript-rendered page: the HTML carries only
                # navigation and the article arrives via a later fetch. Rendering
                # it would need a headless browser, which is not worth it for one
                # club in six — the rest of the league still reports.
                problems.append(f"{article.club}: {article.url} appears to be JS-rendered")
                continue

            documents.append(
                Document(
                    text=text[:MAX_ARTICLE_CHARS],
                    url=article.url,
                    source_name=f"{article.club} official site",
                    # Club articles rarely expose a machine-readable date, and
                    # FPL only links them while they are current — so treating
                    # them as fresh is right. A stale link would have been
                    # replaced on the player's flag.
                    published_at=now,
                    tier=Tier.REPORTED,
                )
            )

    return documents, problems

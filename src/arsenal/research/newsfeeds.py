"""Football news feeds — free, keyless, and Tier 3.

Built as the replacement for Reddit, which now gates API access behind a
Responsible Builder Policy and is no longer straightforwardly self-serve. That
turned out to be a improvement rather than a loss: Reddit was Tier 4 and could
not move a forecast at the default threshold, whereas BBC, Guardian and Sky are
named outlets carrying named journalists, which is Tier 3 and counts.

No API key, no registration, no rate-limit negotiation — just RSS.

The catch is signal density. These feeds carry match reports, live blogs and
transfer gossip alongside the team news that matters, so items are filtered for
relevance before anything reaches the extractor. That is as much about cost as
quality: every irrelevant article is tokens spent to learn nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from .club_news import html_to_text
from .evidence import Tier
from .sources import USER_AGENT, Document

log = logging.getLogger(__name__)

# Free, keyless, and reliably well-formed. All verified live.
DEFAULT_FEEDS: dict[str, str] = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml",
    "The Guardian": "https://www.theguardian.com/football/rss",
    "Sky Sports": "https://www.skysports.com/rss/11661",
}

# Words that mark an item as being about availability rather than about a match
# that has already happened. A match report tells the forecast nothing it cannot
# read from the data; a fitness update tells it something no statistic can.
RELEVANT = (
    "injur",
    "team news",
    "doubt",
    "fitness",
    "return",
    "ruled out",
    "sideline",
    "suspend",
    "ban ",
    "available",
    "recover",
    "knock",
    "setback",
    "press conference",
    "rotat",
    "rest",
    "line-up",
    "lineup",
    "starting xi",
    "predicted",
    "miss",
    "out for",
    "comeback",
    "scan",
    "surgery",
    "hamstring",
    "calf",
    "groin",
    "ankle",
    "thigh",
)

# Items that are almost never about future availability.
EXCLUDE = ("player ratings", "clockwatch", "live!", "- live", "as it happened", "highlights")

_ITEM = re.compile(r"<item>(.*?)</item>", re.DOTALL | re.IGNORECASE)
_TITLE = re.compile(
    r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.DOTALL | re.IGNORECASE
)
_LINK = re.compile(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", re.DOTALL | re.IGNORECASE)
_DESC = re.compile(
    r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", re.DOTALL | re.IGNORECASE
)
_DATE = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL | re.IGNORECASE)


def is_relevant(title: str, description: str) -> bool:
    """Whether an item plausibly concerns player availability.

    Deliberately generous on inclusion and strict on the obvious exclusions.
    Missing a real team-news piece costs signal; letting a match report through
    costs only tokens, and the extractor will find no claims in it anyway.
    """
    text = f"{title} {description}".lower()
    if any(term in text for term in EXCLUDE):
        return False
    return any(term in text for term in RELEVANT)


def _parse_date(raw: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fetch_news_documents(
    feeds: dict[str, str] | None = None,
    *,
    max_age_days: int = 4,
    per_feed: int = 12,
    timeout: float = 20.0,
) -> tuple[list[Document], list[str]]:
    """Fetch recent, availability-relevant football news as documents.

    Returns ``(documents, problems)`` and never raises — one dead feed costs that
    feed's news and nothing else.

    Only the RSS title and summary are used, not the full article. That is a
    deliberate trade: headlines and standfirsts carry the claim ("X ruled out for
    six weeks") densely, while fetching every article body would multiply both
    latency and extraction cost for a modest gain.
    """
    sources = feeds or DEFAULT_FEEDS
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    documents: list[Document] = []
    problems: list[str] = []

    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for outlet, url in sources.items():
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                problems.append(f"{outlet}: {exc}")
                log.info("feed %s failed: %s", outlet, exc)
                continue

            kept = 0
            for block in _ITEM.findall(response.text):
                if kept >= per_feed:
                    break

                title_match = _TITLE.search(block)
                link_match = _LINK.search(block)
                if not title_match or not link_match:
                    continue

                title = html_to_text(title_match.group(1)).strip()
                description = (
                    html_to_text(_DESC.search(block).group(1)).strip()
                    if _DESC.search(block)
                    else ""
                )

                if not is_relevant(title, description):
                    continue

                date_match = _DATE.search(block)
                published = _parse_date(date_match.group(1)) if date_match else None
                if published is None:
                    # Undated items are usually evergreen pages. Without a date
                    # the staleness rules cannot judge them, so skip rather than
                    # guess them fresh.
                    continue
                if published < cutoff:
                    continue

                documents.append(
                    Document(
                        text=f"{title}\n\n{description}".strip()[:4000],
                        url=link_match.group(1).strip(),
                        source_name=outlet,
                        published_at=published,
                        # Named outlets with named journalists. Tier 3 means these
                        # can move a forecast, unlike the Reddit source they
                        # replace.
                        tier=Tier.REPORTED,
                    )
                )
                kept += 1

    log.info("news feeds produced %d relevant documents", len(documents))
    return documents, problems

"""Research: gathering evidence and turning it into forecast adjustments.

The LLM's role is confined to this package, and within it to one job: reading
unstructured prose and deciding what it asserts. It never sees a squad, a budget,
or a price.
"""

from .apply import Adjustment, ResearchReport, apply_to_forecasts, build_report
from .club_news import (
    ClubArticle,
    ScoutRiskSource,
    club_article_links,
    fetch_club_articles,
    html_to_text,
)
from .evidence import (
    Evidence,
    Impact,
    Tier,
    conflicts,
    deduplicate,
    may_adjust_forecast,
)
from .extract import build_client, extract_claims, summarise_evidence
from .resolver import PlayerResolver, Resolution, normalise, resolve_all
from .sources import (
    Document,
    FPLNewsSource,
    SetPieceSource,
    SourceResult,
    fetch_reddit_documents,
    fetch_youtube_documents,
)

__all__ = [
    "Adjustment",
    "ClubArticle",
    "Document",
    "Evidence",
    "FPLNewsSource",
    "Impact",
    "PlayerResolver",
    "ResearchReport",
    "Resolution",
    "ScoutRiskSource",
    "SetPieceSource",
    "SourceResult",
    "Tier",
    "apply_to_forecasts",
    "build_client",
    "build_report",
    "club_article_links",
    "conflicts",
    "deduplicate",
    "extract_claims",
    "fetch_club_articles",
    "fetch_reddit_documents",
    "fetch_youtube_documents",
    "html_to_text",
    "may_adjust_forecast",
    "normalise",
    "resolve_all",
    "summarise_evidence",
]

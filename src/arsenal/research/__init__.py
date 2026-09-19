"""Research: gathering evidence and turning it into forecast adjustments.

The LLM's role is confined to this package, and within it to one job: reading
unstructured prose and deciding what it asserts. It never sees a squad, a budget,
or a price.
"""

from .apply import Adjustment, ResearchReport, apply_to_forecasts, build_report
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
    "Document",
    "Evidence",
    "FPLNewsSource",
    "Impact",
    "PlayerResolver",
    "ResearchReport",
    "Resolution",
    "SetPieceSource",
    "SourceResult",
    "Tier",
    "apply_to_forecasts",
    "build_client",
    "build_report",
    "conflicts",
    "deduplicate",
    "extract_claims",
    "fetch_reddit_documents",
    "fetch_youtube_documents",
    "may_adjust_forecast",
    "normalise",
    "resolve_all",
    "summarise_evidence",
]

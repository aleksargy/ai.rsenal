"""LLM claim extraction.

This is where the language model earns its place: reading prose and deciding
what it actually asserts. It does **not** decide what a player is worth, and it
never sees a squad, a budget, or a price — the solver owns that, and keeping the
model away from it is what makes the system safe to run unattended.

The model's whole job here is to turn "Arteta said Saka trained today and should
be fine" into a structured claim that preserves the hedge, cites its source, and
resolves to an FPL element id — or to return nothing when the text contains no
checkable assertion.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .evidence import Evidence, Tier
from .resolver import PlayerResolver
from .sources import Document

log = logging.getLogger(__name__)

# The skill instruction is to default to the most capable model. Extraction
# quality here directly determines P(plays), which dominates every forecast — a
# misread hedge is worth more points than any amount of optimiser tuning. The
# model is configurable so the cost/quality tradeoff stays the user's call.
DEFAULT_MODEL = "claude-opus-5"

EXTRACTION_SYSTEM = """\
You extract factual claims about Fantasy Premier League players from text.

Your output feeds an automated system that manages a real FPL team. A claim you
invent becomes a transfer that costs real points, so accuracy matters far more
than completeness. Returning nothing is always acceptable and often correct.

Rules:

1. ONE assertion per claim. "He is fit and takes penalties" is two claims with
   different impacts and different verification paths.
2. NEVER invent. Every claim must be supported by the text in front of you. Do
   not add context you happen to know about a player. If the text says nothing
   checkable, return an empty list.
3. PRESERVE HEDGING. "should be available", "is expected to", "might feature" are
   hedged — set hedged=true. Do not promote a hedge into a statement of fact. The
   hedge is information; removing it manufactures confidence the source never had.
4. ATTRIBUTE. If the text quotes a manager, journalist, or official source, put
   that in attributed_to. If it is the author's own speculation, leave it empty.
5. Use the player's name EXACTLY as it appears in the text. Do not correct
   spelling or expand nicknames — a separate resolver handles that, and it needs
   the original.
6. Classify impact honestly:
   - availability: injured, suspended, fit, returning
   - minutes: starting, benched, rotation risk, substitute appearances
   - role: position change, tactical role
   - set_pieces: penalties, free kicks, corners
   - form: performance observations
   - fixture: schedule, congestion, opponent
7. Skip opinion with no factual content. "He's a great differential" and "I'm
   captaining him" are not claims. "He is expected to start against Spurs" is.

Be conservative. A missed claim costs nothing. A fabricated one costs points."""


class ExtractedClaim(BaseModel):
    """One claim as the model read it, before resolution."""

    player_name: str = Field(description="Player name exactly as written in the text")
    club: str = Field(default="", description="Club name or code, if the text gives one")
    claim: str = Field(description="One single factual assertion, stated plainly")
    impact: str = Field(description="availability|minutes|role|set_pieces|form|fixture")
    hedged: bool = Field(description="True if the source hedged rather than asserted")
    attributed_to: str = Field(
        default="", description="Who is quoted, if anyone. Empty for author speculation."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Certainty the claim is correctly read from the text"
    )


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


VALID_IMPACTS: frozenset[str] = frozenset(
    {"availability", "minutes", "role", "set_pieces", "form", "fixture", "price"}
)


def extract_claims(
    client: object,
    documents: list[Document],
    resolver: PlayerResolver,
    *,
    model: str = DEFAULT_MODEL,
    max_documents: int = 40,
) -> tuple[list[Evidence], list[str]]:
    """Extract evidence from documents.

    Returns ``(evidence, problems)``. Problems are reported rather than raised so
    a partial extraction still produces a usable run — and so a run that quietly
    lost half its input is visibly different from one that worked.

    ``client`` is an ``anthropic.Anthropic`` instance. It is typed loosely so the
    package imports cleanly without the optional ``agents`` extra installed.
    """
    evidence: list[Evidence] = []
    problems: list[str] = []
    promoted = 0

    for document in documents[:max_documents]:
        try:
            response = client.messages.parse(  # type: ignore[attr-defined]
                model=model,
                max_tokens=4096,
                system=EXTRACTION_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Source: {document.source_name}\n"
                            f"Published: {document.published_at:%Y-%m-%d}\n\n"
                            f"{document.text}"
                        ),
                    }
                ],
                output_format=ExtractionResult,
            )
            parsed = response.parsed_output
        except Exception as exc:  # one bad document must not end the run
            problems.append(f"{document.url}: {exc}")
            log.warning("extraction failed for %s: %s", document.url, exc)
            continue

        if parsed is None:
            continue

        for claim in parsed.claims:
            if claim.impact not in VALID_IMPACTS:
                problems.append(f"{document.url}: unknown impact {claim.impact!r}")
                continue

            team_id = resolver.resolve_club(claim.club) if claim.club else None
            outcome = resolver.resolve(claim.player_name, team_id=team_id)
            if not outcome.resolved or outcome.element_id is None:
                # Dropped rather than guessed. Misattributing a claim to the
                # wrong player is far worse than losing it.
                problems.append(f"unresolved '{claim.player_name}': {outcome.reason}")
                continue

            # The promotion rule: a source quoting a primary is not itself the
            # evidence — the primary is. A creator repeating a press conference
            # is worth more than a creator's opinion, so an attributed claim
            # rises from Tier 4 to Tier 3.
            tier = document.tier
            if claim.attributed_to and tier == Tier.OPINION:
                tier = Tier.REPORTED
                promoted += 1

            source_name = document.source_name
            if claim.attributed_to:
                source_name = f"{document.source_name} quoting {claim.attributed_to}"

            evidence.append(
                Evidence(
                    player_id=outcome.element_id,
                    team_id=team_id,
                    claim=claim.claim,
                    tier=tier,
                    impact=claim.impact,  # type: ignore[arg-type]
                    source_url=document.url,
                    source_name=source_name,
                    published_at=document.published_at,
                    confidence=claim.confidence,
                    hedged=claim.hedged,
                )
            )

    if promoted:
        log.info("promoted %d claims to Tier 3 by attribution", promoted)
    return evidence, problems


def build_client(api_key: str | None) -> object | None:
    """Construct an Anthropic client, or return None if unavailable.

    An unset ``ANTHROPIC_API_KEY`` does not necessarily mean no credentials — the
    SDK also resolves an ``ant auth login`` profile — so a bare constructor is
    attempted even without an explicit key.
    """
    try:
        import anthropic
    except ImportError:
        log.info("anthropic SDK not installed; install the 'agents' extra")
        return None

    try:
        return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    except Exception as exc:  # missing credentials is a normal state
        log.info("no Anthropic credentials available: %s", exc)
        return None


def summarise_evidence(evidence: list[Evidence]) -> dict[str, int]:
    """Counts by tier and impact, for the run report."""
    summary: dict[str, int] = {}
    for item in evidence:
        summary[f"tier{int(item.tier)}"] = summary.get(f"tier{int(item.tier)}", 0) + 1
        summary[item.impact] = summary.get(item.impact, 0) + 1
    summary["total"] = len(evidence)
    summary["actionable"] = sum(
        1 for e in evidence if e.may_move_forecast and not e.is_stale(now=datetime.now(UTC))
    )
    return summary

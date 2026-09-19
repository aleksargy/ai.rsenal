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
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .evidence import Evidence, Tier
from .providers import ExtractionBackend, build_backend
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
8. You may be given SEVERAL documents in one message, each introduced by a
   `=== DOCUMENT n ===` marker. Set `document` to that number on every claim, so
   each one stays traceable to the source it came from. Never merge documents or
   carry context between them.

Be conservative. A missed claim costs nothing. A fabricated one costs points."""


class ExtractedClaim(BaseModel):
    """One claim as the model read it, before resolution."""

    document: int = Field(
        default=1, description="1-based number of the document this claim came from"
    )
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


# Two failed batches in a row means the provider is down or out of quota, not
# that these documents were awkward. Stopping keeps a doomed run from spending
# minutes of backoff per remaining batch.
GIVE_UP_AFTER = 2

VALID_IMPACTS: frozenset[str] = frozenset(
    {"availability", "minutes", "role", "set_pieces", "form", "fixture", "price"}
)


def extract_claims(
    backend: ExtractionBackend,
    documents: list[Document],
    resolver: PlayerResolver,
    *,
    max_documents: int = 60,
    batch_size: int = 8,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[list[Evidence], list[str]]:
    """Extract evidence from documents.

    Returns ``(evidence, problems)``. Problems are reported rather than raised so
    a partial extraction still produces a usable run — and so a run that quietly
    lost half its input is visibly different from one that worked.

    ``backend`` is whichever model provider is configured — Anthropic or Gemini.
    Nothing below this line knows which, because the job is the same either way:
    read prose, emit checkable claims.

    Documents are sent several per request. That is not an optimisation: Gemini's
    free tier allows only 20 requests per day per model, so one-per-document
    would exhaust nearly three days of quota on a single run. Batching turns ~55
    articles into ~7 requests. Each document is delimited and numbered so every
    claim stays traceable to its source.
    """
    evidence: list[Evidence] = []
    problems: list[str] = []
    promoted = 0

    selected = documents[:max_documents]
    batches = [selected[i : i + batch_size] for i in range(0, len(selected), batch_size)]
    log.info("extracting from %d documents in %d requests", len(selected), len(batches))

    # A batch that fails after exhausting every fallback model means the quota or
    # the service is gone, not that this particular batch was unlucky. Grinding
    # through the remaining batches would add minutes of backoff to learn the
    # same thing, so the run gives up and reports what it has.
    consecutive_failures = 0

    for number, batch in enumerate(batches, start=1):
        prompt = "\n\n".join(
            f"=== DOCUMENT {n} ===\n"
            f"Source: {doc.source_name}\n"
            f"Published: {doc.published_at:%Y-%m-%d}\n\n"
            f"{doc.text}"
            for n, doc in enumerate(batch, start=1)
        )
        label = f"{len(batch)} documents from {batch[0].source_name}"
        try:
            raw_claims = backend.extract(EXTRACTION_SYSTEM, prompt)
        except Exception as exc:  # one bad batch must not end the run
            problems.append(f"{label}: {exc}")
            log.warning("extraction failed for %s: %s", label, exc)
            consecutive_failures += 1
            if consecutive_failures >= GIVE_UP_AFTER:
                problems.append(
                    f"gave up after {consecutive_failures} consecutive failures — "
                    "the model provider is unavailable or out of quota"
                )
                break
            if on_progress:
                on_progress(number, len(batches), len(evidence))
            continue

        consecutive_failures = 0

        for raw in raw_claims:
            try:
                claim = ExtractedClaim.model_validate(raw)
            except Exception as exc:
                problems.append(f"{label}: malformed claim ({exc})")
                continue

            # Map the claim back to the document it came from. An out-of-range
            # index means the model lost track across the batch, and attributing
            # the claim to the wrong article would give it the wrong source, the
            # wrong tier and the wrong date - so it is dropped.
            index = claim.document - 1
            if not 0 <= index < len(batch):
                problems.append(
                    f"{label}: claim cited document {claim.document}, "
                    "which is not in this batch"
                )
                continue
            document = batch[index]

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

        if on_progress:
            on_progress(number, len(batches), len(evidence))

    if promoted:
        log.info("promoted %d claims to Tier 3 by attribution", promoted)
    return evidence, problems


def build_client(
    anthropic_key: str | None,
    *,
    provider: str = "auto",
    model: str | None = None,
    gemini_key: str | None = None,
) -> ExtractionBackend | None:
    """Select an extraction backend, or None when nothing is configured.

    Returning None is a normal state, not a failure: the pipeline then runs on
    Tier 1 data alone, which still catches every official injury and suspension.
    """
    return build_backend(
        provider=provider,
        model=model,
        anthropic_key=anthropic_key,
        gemini_key=gemini_key,
    )


def summarise_evidence(evidence: list[Evidence]) -> dict[str, int]:
    """Counts by tier and impact, for the run report."""
    summary: dict[str, int] = {}
    for item in evidence:
        summary[f"tier{int(item.tier)}"] = summary.get(f"tier{int(item.tier)}", 0) + 1
        summary[item.impact] = summary.get(item.impact, 0) + 1
    summary["total"] = len(evidence)
    summary["actionable"] = sum(
        1 for e in evidence if e.may_move_forecast() and not e.is_stale(now=datetime.now(UTC))
    )
    return summary

"""``arsenal sources`` — what research is configured, and what it costs.

Setup spans four credentials across four providers, and a half-configured
pipeline fails quietly: it simply gathers less and says nothing. This lists every
source, whether it is ready, and exactly what to do about the ones that are not.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Column, Table

from .config import Config

console = Console()

# Measured from a real run: ~51k input and ~18k output tokens across ~45 club
# articles plus community documents.
RUN_INPUT_TOKENS = 51_000
RUN_OUTPUT_TOKENS = 18_000

PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Google AI Studio has a real free tier with limits that comfortably fit a
    # research run of ~60 documents.
    "gemini-2.5-flash": (0.0, 0.0),
    "gemini-2.5-pro": (0.0, 0.0),
}

# The scheduled agent researches at T-72h, T-24h and T-3h, plus ad-hoc runs.
RUNS_PER_GAMEWEEK = 4
GAMEWEEKS = 38


def _cost(model: str) -> tuple[float, float]:
    inp, out = PRICING.get(model, PRICING["claude-opus-5"])
    per_run = RUN_INPUT_TOKENS / 1e6 * inp + RUN_OUTPUT_TOKENS / 1e6 * out
    return per_run, per_run * RUNS_PER_GAMEWEEK * GAMEWEEKS


def sources(
    test: Annotated[
        bool, typer.Option("--test", help="Make one live call to prove extraction works")
    ] = False,
) -> None:
    """Show which research sources are configured and what is missing."""
    config = Config.load()
    secrets = config.secrets

    rows = [
        (
            "FPL player status",
            "1",
            True,
            "injuries, suspensions, availability",
            "",
        ),
        (
            "FPL set-piece order",
            "1",
            True,
            "penalty and dead-ball duty",
            "",
        ),
        (
            "FPL scout risks",
            "1",
            True,
            "gameweek-scoped loan ineligibility",
            "",
        ),
        (
            "Official club news",
            "3",
            bool(secrets.anthropic_api_key or secrets.gemini_api_key),
            "~45 press-conference articles",
            "needs ANTHROPIC_API_KEY or GEMINI_API_KEY (free) to read the prose",
        ),
        (
            "Football news feeds",
            "3",
            bool(secrets.anthropic_api_key or secrets.gemini_api_key),
            "BBC, Guardian, Sky team news",
            "needs a model key to read the prose; the feeds themselves are free",
        ),
        (
            "Reddit (optional)",
            "4",
            bool(secrets.reddit_client_id and secrets.reddit_client_secret)
            and bool(secrets.anthropic_api_key or secrets.gemini_api_key),
            "late team news, ownership mood",
            _reddit_hint(secrets),
        ),
        (
            "YouTube creators",
            "4",
            bool(secrets.youtube_api_key)
            and bool(config.research.youtube_channels)
            and bool(secrets.anthropic_api_key or secrets.gemini_api_key),
            "transcripts, hypotheses to verify",
            _youtube_hint(secrets, config),
        ),
    ]

    table = Table(
        Column("source", min_width=20),
        "tier",
        "status",
        Column("what it adds", min_width=26),
        title="Research sources",
        title_justify="left",
    )
    for name, tier, ready, adds, _ in rows:
        table.add_row(
            name, tier, "[green]ready[/green]" if ready else "[yellow]off[/yellow]", adds
        )
    console.print(table)

    missing = [(name, hint) for name, _, ready, _, hint in rows if not ready and hint]
    if missing:
        console.print("\n[bold]To enable the rest[/bold]\n")
        for name, hint in missing:
            console.print(f"  [yellow]{name}[/yellow]: {hint}")

    tier1_only = not (secrets.anthropic_api_key or secrets.gemini_api_key)
    if tier1_only:
        console.print(
            "\n[dim]Without a model key the agent still runs on Tier 1 data — "
            "official injuries, suspensions and set-piece duty. What it cannot do "
            "is read a press conference, which is the one thing statistics cannot "
            "supply.[/dim]"
        )

    from .research import build_backend

    backend = build_backend(
        provider=config.research.provider,
        model=config.research.model,
        anthropic_key=secrets.anthropic_api_key,
        gemini_key=secrets.gemini_api_key,
    )
    model = backend.model if backend else (config.research.model or "none configured")
    console.print(f"\n[bold]Extraction cost[/bold] — currently [cyan]{model}[/cyan]\n")
    costs = Table("model", "per run", f"per season ({RUNS_PER_GAMEWEEK} runs/GW)", "")
    for candidate in PRICING:
        run_cost, season_cost = _cost(candidate)
        marker = "  [cyan]<- configured[/cyan]" if candidate == model else ""
        if candidate.startswith("gemini"):
            costs.add_row(candidate, "[green]free[/green]", "[green]free[/green]", marker)
        else:
            costs.add_row(candidate, f"${run_cost:.2f}", f"${season_cost:,.0f}", marker)
    console.print(costs)
    console.print(
        "[dim]Change it with research.model in config.yaml. Extraction quality "
        "sets P(plays), which dominates the forecast — hence the capable "
        "default.[/dim]"
    )

    if test:
        _test_extraction(backend)


def _test_extraction(backend) -> None:
    """Prove the model backend works by extracting from a known passage.

    "It ran without erroring" is not the same as "it understood the text", so
    this uses a passage with a known answer: one firm claim, one hedged one, both
    attributed. If hedging is lost the extraction is not trustworthy, because
    "should be fit" and "is fit" carry very different weight downstream.
    """
    from .research.extract import EXTRACTION_SYSTEM

    if backend is None:
        console.print(
            "\n[yellow]no model configured[/yellow] — set ANTHROPIC_API_KEY or "
            "GEMINI_API_KEY to test extraction."
        )
        return

    passage = (
        "=== DOCUMENT 1 ===\n"
        "Source: Test FC official site\n"
        "Published: 2026-09-19\n\n"
        "Manager Pep Guardiola confirmed that Erling Haaland will miss Saturday's "
        "match with a hamstring injury. He added that Phil Foden should be "
        "available, though a late fitness test is planned."
    )

    console.print(f"\n[bold]Testing extraction[/bold] with {backend.name}/{backend.model}...")
    try:
        claims = backend.extract(EXTRACTION_SYSTEM, passage)
    except Exception as exc:
        console.print(f"[red]failed[/red]: {str(exc)[:200]}")
        return

    if not claims:
        console.print("[yellow]returned no claims[/yellow] — the passage contains two.")
        return

    table = Table("player", "claim", "hedged", "attributed to")
    for claim in claims:
        table.add_row(
            claim.get("player_name", "?"),
            claim.get("claim", "")[:46],
            "yes" if claim.get("hedged") else "no",
            claim.get("attributed_to", "") or "[dim]—[/dim]",
        )
    console.print(table)

    hedges = {bool(c.get("hedged")) for c in claims}
    attributed = any(c.get("attributed_to") for c in claims)
    if hedges == {True, False} and attributed:
        console.print(
            "[green]working[/green] — it separated the firm claim from the hedged "
            "one and kept the attribution, which is what promotes a claim to Tier 3."
        )
    else:
        console.print(
            "[yellow]working, but imprecise[/yellow] — it should mark Haaland firm, "
            "Foden hedged, and attribute both to Guardiola. Consider a stronger model."
        )


def _reddit_hint(secrets) -> str:
    if not secrets.reddit_client_id or not secrets.reddit_client_secret:
        return (
            "API access now sits behind Reddit's Responsible Builder Policy and "
            "is no longer reliably self-serve. Skip it — it is Tier 4 and cannot "
            "move a forecast at the default threshold. The news feeds above cover "
            "the same ground at Tier 3."
        )
    if not (secrets.anthropic_api_key or secrets.gemini_api_key):
        return "credentials present, but reading the posts needs a model key"
    return ""


def _youtube_hint(secrets, config) -> str:
    parts = []
    if not secrets.youtube_api_key:
        parts.append(
            "enable YouTube Data API v3 in the Google Cloud console and set "
            "YOUTUBE_API_KEY (free tier is ample)"
        )
    if not config.research.youtube_channels:
        parts.append("add channel ids under research.youtube_channels in config.yaml")
    if not (secrets.anthropic_api_key or secrets.gemini_api_key):
        parts.append("reading transcripts needs ANTHROPIC_API_KEY or GEMINI_API_KEY")
    if parts:
        parts.append("transcripts also need: uv pip install youtube-transcript-api")
    return "; ".join(parts)


def main() -> None:  # pragma: no cover - thin CLI wrapper
    typer.run(sources)

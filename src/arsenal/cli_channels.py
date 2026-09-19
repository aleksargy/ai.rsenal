"""``arsenal channels`` — finding YouTube channel ids for FPL creators.

A channel id is a `UC...` string that is deliberately hard to find in the
YouTube interface: the URL usually shows a handle like `@FPLHarry` instead. This
searches for them so setting up creator sources is a copy-paste rather than an
expedition.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Column, Table

from .config import Config

console = Console()

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# A starting point rather than a recommendation: these are widely followed FPL
# channels, and the search below confirms whichever actually exist. Calibration
# is what should decide who you keep — `arsenal backtest` after a few gameweeks
# says more than any list.
SUGGESTED = (
    "Let's Talk FPL",
    "FPL Harry",
    "FPL Mate",
    "Fantasy Football Scout",
    "FPL Focal",
    "Planet FPL",
)


def channels(
    query: Annotated[str | None, typer.Argument(help="Channel name to search for")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Results per search")] = 3,
) -> None:
    """Find YouTube channel ids to put in config.yaml.

    With no argument, searches a handful of well-known FPL channels. Pass a name
    to search for a specific one.
    """
    config = Config.load()
    key = config.secrets.youtube_api_key
    if not key:
        console.print(
            "[yellow]YOUTUBE_API_KEY is not set.[/yellow]\n\n"
            "  1. https://console.cloud.google.com → new project\n"
            "  2. Enable [bold]YouTube Data API v3[/bold]\n"
            "  3. Credentials → Create credentials → API key\n"
            "  4. Put it in .env as YOUTUBE_API_KEY\n\n"
            "[dim]Free tier is 10,000 quota units a day; a research run uses a "
            "handful.[/dim]"
        )
        raise typer.Exit(1)

    queries = [query] if query else list(SUGGESTED)
    table = Table(
        Column("channel", min_width=22),
        Column("channel id", min_width=24),
        "subscribers",
        title="YouTube channels",
        title_justify="left",
    )

    found: list[str] = []
    with httpx.Client(timeout=20.0) as client:
        for term in queries:
            try:
                response = client.get(
                    SEARCH_URL,
                    params={
                        "key": key,
                        "q": term,
                        "part": "snippet",
                        "type": "channel",
                        "maxResults": limit,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                console.print(f"[red]search for '{term}' failed[/red]: {exc}")
                continue

            for item in response.json().get("items", []):
                snippet = item.get("snippet", {})
                channel_id = snippet.get("channelId") or item.get("id", {}).get("channelId")
                if not channel_id:
                    continue
                table.add_row(snippet.get("title", "?"), channel_id, "")
                found.append(channel_id)
                break  # the first hit per search term is almost always the right one

    console.print(table)

    if found:
        console.print("\n[bold]Add to config.yaml under research.youtube_channels:[/bold]\n")
        for channel_id in found:
            console.print(f"    - {channel_id}")
        console.print(
            '\n[dim]Then: uv pip install -e ".[youtube]" for transcripts. '
            "Creator claims are Tier 4 — their judgement about rotation and role "
            "carries real weight, their relaying of an injury does not.[/dim]"
        )

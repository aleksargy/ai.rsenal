"""``arsenal transcripts`` — harvesting captions where that is possible.

Splits the one job that genuinely needs a residential IP away from everything
else. YouTube blocks cloud ranges outright, so a hosted run can never fetch a
transcript — but it can happily *read* one that was fetched earlier.

Transcripts are public, immutable and small, which makes them ideal for this:
harvest them locally whenever the machine happens to be on, commit them, and let
the scheduled cloud run use whatever is in the repository. A week with the laptop
shut costs freshness, not the run.

Because they are committed, they also have to be cleaned up. Creators publish
several times a week, so an unpruned archive grows by hundreds of files a season
and every one of them is dead weight the moment it falls outside the search
window. Each harvest therefore ends by dropping whatever has aged out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Config
from .research.community import DEFAULT_TRANSCRIPT_CACHE
from .research.transcripts import DEFAULT_RETENTION_DAYS, TranscriptFetcher

console = Console()


def transcripts(
    max_age_days: Annotated[
        int, typer.Option("--days", help="How far back to look for videos")
    ] = 7,
    per_channel: Annotated[int, typer.Option("--per-channel", help="Videos per channel")] = 3,
    keep_days: Annotated[
        int,
        typer.Option("--keep-days", help="Delete cached transcripts older than this"),
    ] = DEFAULT_RETENTION_DAYS,
    no_prune: Annotated[
        bool, typer.Option("--no-prune", help="Keep aged-out transcripts on disk")
    ] = False,
    prune_only: Annotated[
        bool, typer.Option("--prune-only", help="Drop stale transcripts, fetch nothing")
    ] = False,
) -> None:
    """Fetch and cache YouTube transcripts for recent creator videos.

    Run on a machine with a residential IP. Everything fetched is cached
    permanently and committed, so the scheduled run can use it from anywhere.
    """
    config = Config.load()
    fetcher = TranscriptFetcher(
        DEFAULT_TRANSCRIPT_CACHE, proxy=config.research.transcript_proxy
    )

    if prune_only:
        _prune(fetcher, keep_days)
        raise typer.Exit(0)

    if not config.secrets.youtube_api_key:
        console.print(
            "[yellow]YOUTUBE_API_KEY is not set[/yellow] — nothing to harvest.\n"
            "See `arsenal sources` for setup."
        )
        raise typer.Exit(0)
    if not config.research.youtube_channels:
        console.print(
            "[yellow]no channels configured[/yellow] — add ids under "
            "research.youtube_channels in config.yaml.\n"
            'Find them with `arsenal channels "<name>"`.'
        )
        raise typer.Exit(0)

    import httpx

    before = fetcher.cached_count
    published_after = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()

    videos: list[tuple[str, str, str, datetime]] = []
    with httpx.Client(timeout=20.0) as client:
        for channel_id in config.research.youtube_channels:
            try:
                response = client.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={
                        "key": config.secrets.youtube_api_key,
                        "channelId": channel_id,
                        "part": "snippet",
                        "order": "date",
                        "maxResults": per_channel,
                        "type": "video",
                        "publishedAfter": published_after,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                console.print(f"[yellow]channel {channel_id} failed[/yellow]: {exc}")
                continue
            for item in response.json().get("items", []):
                video_id = item.get("id", {}).get("videoId")
                if not video_id:
                    continue
                snippet = item.get("snippet", {})
                try:
                    published = datetime.fromisoformat(
                        snippet["publishedAt"].replace("Z", "+00:00")
                    )
                except (KeyError, ValueError):
                    published = datetime.now(UTC)
                videos.append(
                    (
                        video_id,
                        snippet.get("channelTitle", "?"),
                        snippet.get("title", "")[:52],
                        published,
                    )
                )

    console.print(
        f"{len(videos)} recent videos · {before} already cached · "
        f"pacing requests to avoid a block\n"
    )

    table = Table("channel", "video", "age", "result")
    fetched = 0
    now = datetime.now(UTC)
    for video_id, channel, title, published in videos:
        age = f"{(now - published).days}d"
        cached, _ = fetcher.cache.get(video_id)
        if cached:
            table.add_row(channel[:18], title, age, "[dim]cached[/dim]")
            continue

        text = fetcher.fetch(video_id, published_at=published, title=title, channel=channel)
        if fetcher.blocked:
            table.add_row(channel[:18], title, age, "[red]blocked[/red]")
            break
        if text:
            fetched += 1
            table.add_row(channel[:18], title, age, f"[green]{len(text):,} chars[/green]")
        else:
            table.add_row(channel[:18], title, age, "[dim]no captions[/dim]")

    console.print(table)

    if fetcher.blocked:
        console.print(f"\n[yellow]{fetcher.blocked}[/yellow]")

    removed = [] if no_prune else fetcher.prune(keep_days)
    console.print(
        f"\n[green]{fetched} new[/green] · {len(removed)} pruned · "
        f"{fetcher.cached_count} cached in total"
    )
    _report_spread(fetcher)

    if fetched or removed:
        console.print(
            "\n[dim]Commit data/transcripts/ so the scheduled run can use them. "
            "The workflow does this automatically.[/dim]"
        )


def _prune(fetcher: TranscriptFetcher, keep_days: int) -> None:
    removed = fetcher.prune(keep_days)
    console.print(
        f"[green]{len(removed)} pruned[/green] (older than {keep_days}d) · "
        f"{fetcher.cached_count} remaining"
    )
    _report_spread(fetcher)


def _report_spread(fetcher: TranscriptFetcher) -> None:
    """Show how old the archive is, so a stale cache cannot pass as current."""
    ages = fetcher.cache.ages()
    if not ages:
        console.print("[dim]cache is empty[/dim]")
        return
    console.print(f"[dim]newest {ages[0][1]:.1f}d old · oldest {ages[-1][1]:.1f}d old[/dim]")

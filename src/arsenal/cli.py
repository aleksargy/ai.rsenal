"""``arsenal`` command line.

``doctor`` first — it probes every read endpoint and reports API drift, which is
the fastest way to tell whether a failure is your code or the season having moved
underneath it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Config
from .fpl.client import AuthRequired, FPLClient, FPLError
from .fpl.rules import available_chips, valid_formations
from .money import format_money

app = typer.Typer(
    name="arsenal",
    help="Autonomous Fantasy Premier League manager.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _client(config: Config, *, gameweek: int | None = None) -> FPLClient:
    raw_dir = config.run_dir(gameweek) / "raw" if gameweek is not None else None
    return FPLClient(
        config.cache_dir,
        session_cookies=config.secrets.session_cookies or None,
        raw_dir=raw_dir,
    )


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def doctor() -> None:
    """Probe every FPL endpoint and report drift, cache state and session health.

    Run this first when anything breaks. The FPL API is unversioned and changes
    shape between seasons, so "my code broke" and "the API moved" look identical
    until you check.
    """
    config = Config.load()
    console.print("[bold]ai.rsenal doctor[/bold]\n")

    endpoints = [
        ("bootstrap-static", lambda c: c.get("/bootstrap-static/", cache_key="bootstrap")),
        ("fixtures", lambda c: c.get("/fixtures/", cache_key="fixtures")),
        ("event-status", lambda c: c.get("/event-status/", ttl=0)),
        ("set-piece-notes", lambda c: c.get("/team/set-piece-notes/", cache_key="spn")),
    ]

    table = Table("endpoint", "status", "detail")
    with _client(config) as client:
        for name, fetch in endpoints:
            try:
                payload = fetch(client)
                size = len(json.dumps(payload))
                table.add_row(name, "[green]ok[/green]", f"{size:,} bytes")
            except FPLError as exc:
                table.add_row(name, "[red]fail[/red]", str(exc)[:70])

        console.print(table)

        # Schema drift: the defensive parse boundary is where a season change bites.
        console.print()
        try:
            bootstrap = client.bootstrap()
            console.print(
                f"[green]schema ok[/green] — {len(bootstrap.elements)} players, "
                f"{len(bootstrap.teams)} teams, {len(bootstrap.events)} gameweeks"
            )
            rules = bootstrap.game_config.rules
            budget = format_money(rules.squad_total_spend)
            console.print(
                f"  squad {rules.squad_squadsize} · XI {rules.squad_squadplay} · "
                f"max {rules.squad_team_limit}/club · budget {budget} · "
                f"max {rules.max_free_transfers} banked FT"
            )
            formations = sorted(valid_formations())
            console.print(
                "  legal formations: " + ", ".join("-".join(map(str, f)) for f in formations)
            )
            nxt = bootstrap.next_event
            if nxt:
                delta = nxt.deadline_time - datetime.now(UTC)
                hours = delta.total_seconds() / 3600
                console.print(
                    f"\n[bold]next deadline[/bold] GW{nxt.id} "
                    f"{nxt.deadline_time:%Y-%m-%d %H:%M UTC} "
                    f"([cyan]T−{hours:.1f}h[/cyan])"
                )
        except Exception as exc:
            console.print(f"[red]schema drift[/red]: {exc}")
            console.print("  A payload no longer matches src/arsenal/fpl/schemas.py.")

        # Session health
        console.print()
        if not config.secrets.team_id:
            console.print("[yellow]FPL_TEAM_ID is not set[/yellow] — cannot check session")
        elif not config.secrets.has_session:
            console.print(
                "[yellow]no session[/yellow] — reads work, writes will not. "
                "Seed one with `arsenal auth login`."
            )
        elif client.is_authenticated(config.secrets.team_id):
            console.print("[green]session ok[/green] — authenticated writes available")
        else:
            console.print(
                "[red]session expired[/red] — the agent will run in advisory mode. "
                "Re-seed with `arsenal auth login`."
            )


@app.command()
def rules(refresh: Annotated[bool, typer.Option("--refresh")] = False) -> None:
    """Show the live scoring and rules config, and cache it to data/reference/.

    These are the numbers the FPL engine actually runs on. Prefer them over any
    documentation — scoring rules change most seasons.
    """
    config = Config.load()
    with _client(config) as client:
        bootstrap = client.bootstrap(ttl=0 if refresh else None)

    scoring = bootstrap.game_config.scoring
    positions = ["GKP", "DEF", "MID", "FWD"]

    table = Table("event", *positions, title="Scoring")
    table.add_row("Playing 60+ min", *[str(scoring.long_play)] * 4)
    table.add_row("Playing 1-59 min", *[str(scoring.short_play)] * 4)
    table.add_row("Goal", *[str(scoring.goals_scored.get(p, 0)) for p in positions])
    table.add_row("Assist", *[str(scoring.assists)] * 4)
    table.add_row("Clean sheet", *[str(scoring.clean_sheets.get(p, 0)) for p in positions])
    table.add_row(
        "Defensive contribution",
        *[str(scoring.defensive_contribution.get(p, 0)) for p in positions],
    )
    table.add_row("Per 2 conceded", *[str(scoring.goals_conceded.get(p, 0)) for p in positions])
    table.add_row("Yellow / red", *[f"{scoring.yellow_cards} / {scoring.red_cards}"] * 4)
    console.print(table)

    console.print(
        "\n[dim]Defensive contribution is a threshold, not a rate: 10+ CBIT for "
        "defenders, 12+ CBIT plus recoveries for midfielders and forwards. "
        "Expected value is 2 x P(actions >= threshold).[/dim]"
    )

    chips = Table("chip", "type", "window", title="Chips")
    for chip in bootstrap.chips:
        chips.add_row(chip.name, chip.chip_type, f"GW{chip.start_event}-{chip.stop_event}")
    console.print(chips)

    if refresh:
        config.reference_dir.mkdir(parents=True, exist_ok=True)
        target = config.reference_dir / "game_config.json"
        target.write_text(bootstrap.game_config.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"\n[green]wrote[/green] {target}")


@app.command()
def status() -> None:
    """Show your current squad, bank, free transfers and remaining chips."""
    config = Config.load()
    if not config.secrets.team_id:
        console.print("[red]FPL_TEAM_ID is not set.[/red]")
        raise typer.Exit(1)

    with _client(config) as client:
        bootstrap = client.bootstrap()
        elements = bootstrap.element_by_id()
        teams = bootstrap.team_by_id()
        nxt = bootstrap.next_event
        gameweek = nxt.id if nxt else 1

        try:
            team = client.my_team(config.secrets.team_id)
        except AuthRequired:
            console.print(
                "[red]session expired or missing[/red] — cannot read your squad.\n"
                "Public data still works; re-seed with `arsenal auth login`."
            )
            raise typer.Exit(2) from None

        table = Table("pos", "player", "club", "price", "sells for", "role")
        for pick in sorted(team.picks, key=lambda p: p.position):
            element = elements.get(pick.element)
            if element is None:
                continue
            role = "C" if pick.is_captain else "V" if pick.is_vice_captain else ""
            if pick.position > 11:
                role = f"bench {pick.position - 11}"
            table.add_row(
                element.element_type.short,
                element.name,
                teams[element.team].short_name if element.team in teams else "?",
                format_money(element.now_cost),
                format_money(pick.selling_price),
                role,
            )
        console.print(table)

        selling_value = sum(p.selling_price for p in team.picks)
        free = team.transfers.limit if team.transfers.limit is not None else "-"
        console.print(
            f"\nbank {format_money(team.transfers.bank)} · "
            f"selling value {format_money(selling_value)} · "
            f"budget {format_money(selling_value + team.transfers.bank)} · "
            f"free transfers {free}"
        )

        used = [
            str(c.get("name", "")) for c in team.chips if c.get("status_for_entry") == "played"
        ]
        remaining = available_chips(bootstrap, used, gameweek)
        console.print(f"chips available in GW{gameweek}: {', '.join(remaining) or 'none'}")


@app.command()
def deadline() -> None:
    """Show the next deadline and which pipeline stage is due."""
    config = Config.load()
    with _client(config) as client:
        bootstrap = client.bootstrap()

    nxt = bootstrap.next_event
    if nxt is None:
        console.print("[yellow]no upcoming gameweek — the season may be over[/yellow]")
        raise typer.Exit(0)

    remaining = nxt.deadline_time - datetime.now(UTC)
    hours = remaining.total_seconds() / 3600
    schedule = config.schedule

    if hours < 0:
        due = "deadline passed — waiting for the next gameweek"
    elif hours * 60 < schedule.abort_if_under_minutes:
        due = "too late to submit safely — holding"
    elif hours * 60 <= schedule.submit_minutes_before:
        due = "validate + execute"
    elif hours <= schedule.final_research_hours_before:
        due = "final news sweep, re-forecast, re-optimise"
    elif hours <= schedule.provisional_hours_before:
        due = "forecast + optimise, notify provisional plan"
    elif hours <= schedule.research_hours_before:
        due = "snapshot + full research fan-out"
    else:
        due = "idle"

    console.print(
        f"[bold]GW{nxt.id}[/bold] deadline {nxt.deadline_time:%Y-%m-%d %H:%M UTC}\n"
        f"T−{hours:.1f}h → [cyan]{due}[/cyan]"
    )


if __name__ == "__main__":
    app()

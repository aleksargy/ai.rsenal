"""``arsenal`` command line.

``doctor`` first — it probes every read endpoint and reports API drift, which is
the fastest way to tell whether a failure is your code or the season having moved
underneath it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from itertools import zip_longest
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import Config
from .fpl.client import AuthRequired, FPLClient, FPLError
from .fpl.rules import SquadPlayer, available_chips, valid_formations, validate_squad
from .money import format_money
from .optimizer import OptimiserConfig, OptimiserError, build_candidates, optimise

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


@app.command()
def plan(
    horizon: Annotated[int, typer.Option("--horizon", "-h", help="Gameweeks to plan over")] = 0,
    chip: Annotated[
        str | None, typer.Option("--chip", help="Force a chip this gameweek")
    ] = None,
    fresh: Annotated[
        bool, typer.Option("--fresh", help="Build from scratch on a full budget")
    ] = False,
    max_hit: Annotated[
        int | None, typer.Option("--max-hit", help="Points spendable on hits")
    ] = None,
) -> None:
    """Solve for the best squad and print the proposed gameweek.

    Uses the placeholder forecast from ``optimizer.baseline`` — good enough to
    exercise the solver end to end, not yet good enough to trust unattended.
    M3 replaces it.
    """
    config = Config.load()
    settings = config.optimiser

    with _client(config) as client:
        bootstrap = client.bootstrap()
        elements = bootstrap.element_by_id()
        teams = bootstrap.team_by_id()
        nxt = bootstrap.next_event

        my_team = None
        if not fresh and config.secrets.team_id:
            try:
                my_team = client.my_team(config.secrets.team_id)
            except AuthRequired:
                console.print(
                    "[yellow]no valid session[/yellow] — planning a fresh squad "
                    "on a full budget instead of optimising your actual team.\n"
                )

    # Without a squad to start from there is nothing to transfer out of, so the
    # only coherent framing is a wildcard: 15 purchases on the full budget.
    building_fresh = my_team is None
    if building_fresh:
        bank = bootstrap.game_config.rules.squad_total_spend
        free_transfers = 1
        chip = chip or "wildcard"
    else:
        # Only cash in hand: the value of the current squad enters the model
        # through the sell variables, at selling rather than current price.
        bank = my_team.transfers.bank
        free_transfers = my_team.transfers.limit or 1

    opt_config = OptimiserConfig(
        horizon=horizon or settings.horizon,
        discount=settings.discount,
        risk_aversion=settings.risk_aversion,
        bench_weight=settings.bench_weight,
        max_hit=settings.max_hit if max_hit is None else max_hit,
        max_free_transfers=bootstrap.game_config.rules.max_free_transfers,
        solver_time_limit=settings.solver_time_limit,
        squad_requirements=bootstrap.squad_requirements(),
        play_limits=bootstrap.play_limits(),
    )

    candidates = build_candidates(bootstrap, my_team=my_team, horizon=opt_config.horizon)
    console.print(
        f"[dim]pool: {len(candidates)} players · horizon {opt_config.horizon} GW · "
        f"bank {format_money(bank)} · {free_transfers} free transfer(s)"
        + (f" · chip {chip}" if chip else "")
        + "[/dim]\n"
    )

    try:
        result = optimise(
            candidates,
            initial_bank=bank,
            initial_free_transfers=free_transfers,
            config=opt_config,
            chip=chip,
        )
    except OptimiserError as exc:
        console.print(f"[red]optimiser failed[/red]: {exc}")
        raise typer.Exit(1) from exc

    index = {c.element_id: c for c in candidates}
    decision = result.this_week
    gameweek = nxt.id if nxt else "?"

    def describe(element_id: int) -> tuple[str, str, str, str]:
        element = elements[element_id]
        return (
            element.element_type.short,
            element.name,
            teams[element.team].short_name if element.team in teams else "?",
            format_money(element.now_cost),
        )

    table = Table("pos", "player", "club", "price", "xP", title=f"GW{gameweek} starting XI")
    for element_id in sorted(decision.starting, key=lambda i: index[i].position):
        position, name, club, price = describe(element_id)
        if element_id == decision.captain:
            name = f"[bold]{name} (C)[/bold]"
        elif element_id == decision.vice_captain:
            name = f"{name} (V)"
        table.add_row(position, name, club, price, f"{index[element_id].points(0):.2f}")
    console.print(table)

    bench = Table("#", "pos", "player", "club", "price", "xP", title="Bench")
    for order, element_id in enumerate(decision.bench, start=1):
        position, name, club, price = describe(element_id)
        bench.add_row(
            str(order), position, name, club, price, f"{index[element_id].points(0):.2f}"
        )
    console.print(bench)

    if decision.transfers_in or decision.transfers_out:
        transfers = Table("out", "in", title="Transfers")
        outgoing = sorted(decision.transfers_out)
        incoming = sorted(decision.transfers_in)
        for out_id, in_id in zip_longest(outgoing, incoming):
            transfers.add_row(
                describe(out_id)[1] if out_id else "—",
                describe(in_id)[1] if in_id else "—",
            )
        console.print(transfers)
    else:
        console.print("[dim]no transfers this gameweek[/dim]")

    cost = f", costing [red]-{decision.hit_cost}[/red]" if decision.hits else ""
    console.print(
        f"\n{len(decision.transfers_in)} transfer(s){cost} · "
        f"bank {format_money(decision.bank)} · "
        f"xP this GW [bold]{decision.expected_points:.1f}[/bold] · "
        f"xP over {opt_config.horizon} GW [bold]{result.total_expected_points:.1f}[/bold]"
    )

    # Cross-check the solver against the independently written validator. A bug
    # shared between the two would be invisible, which is the whole point of not
    # sharing their code.
    bench_rank = {pid: rank + 1 for rank, pid in enumerate(decision.bench)}
    squad_players = [
        SquadPlayer(
            element_id=pid,
            position=index[pid].position,
            team=index[pid].team,
            now_cost=index[pid].now_cost,
            purchase_price=index[pid].purchase_price,
            is_starting=pid in decision.starting,
            is_captain=pid == decision.captain,
            is_vice_captain=pid == decision.vice_captain,
            bench_rank=bench_rank.get(pid),
        )
        for pid in decision.squad
    ]
    spent = sum(p.sells_for for p in squad_players)
    validation = validate_squad(
        squad_players,
        budget=spent + decision.bank,
        squad_requirements=bootstrap.squad_requirements(),
        play_limits=bootstrap.play_limits(),
        elements=elements,
    )
    if validation.ok:
        console.print("[green]validated[/green] — squad satisfies every rule invariant")
    else:
        console.print(f"\n[red]VALIDATION FAILED[/red]\n{validation}")
        raise typer.Exit(2)
    for warning in validation.warnings:
        console.print(f"[yellow]warn[/yellow] {warning}")

    console.print(
        "\n[dim]Forecast is the M3 placeholder: FPL's own ep_next blended with a "
        "shrunk season rate. No research, fixtures, or team news yet.[/dim]"
    )


if __name__ == "__main__":
    app()

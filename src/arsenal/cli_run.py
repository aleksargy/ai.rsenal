"""``arsenal run`` — the whole pipeline, end to end, writing nothing.

Everything the scheduled agent will do at a deadline, in one command, stopping
at the point where it would submit. The output is the summary you would get in a
notification, so what you read here is exactly what the automated version will
send.

Writing is not merely disabled by a flag — this command has no write path at all.
That is deliberate: a dry run whose safety depends on a flag being set correctly
is one typo away from not being a dry run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Column, Table

from .config import Config
from .forecast import build_league_model, forecast_players, load_history, upcoming_fixtures
from .fpl.client import AuthRequired
from .fpl.rules import SquadPlayer, available_chips, validate_squad
from .money import format_money
from .optimizer import OptimiserConfig, OptimiserError, candidates_from_forecasts, optimise
from .research import Tier, apply_to_forecasts, build_report, summarise_evidence
from .session import authenticated_client

console = Console()


def run(
    horizon: Annotated[int, typer.Option("--horizon", help="Gameweeks to plan over")] = 0,
    llm: Annotated[
        bool | None,
        typer.Option("--llm/--no-llm", help="Extract claims from prose with Claude"),
    ] = None,
    chip: Annotated[str | None, typer.Option("--chip", help="Force a chip")] = None,
) -> None:
    """Run the full pipeline and print what the agent would do. Writes nothing.

    By default claim extraction runs when an Anthropic key is configured and is
    skipped when it is not, so the command does the most it can with whatever you
    have set up rather than failing or silently doing less.
    """
    config = Config.load()
    settings = config.optimiser
    started = datetime.now(UTC)

    # Default to whatever the setup supports, rather than forcing a choice.
    has_key = bool(config.secrets.anthropic_api_key or config.secrets.gemini_api_key)
    use_llm = has_key if llm is None else llm

    console.rule("[bold]snapshot")
    with authenticated_client(config, required=False) as client:
        bootstrap = client.bootstrap()
        elements = bootstrap.element_by_id()
        teams = bootstrap.team_by_id()
        raw_elements = client.raw_elements()
        history = load_history(client, bootstrap)
        fixtures = client.fixtures()
        nxt = bootstrap.next_event

        my_team = None
        auth_note = "[yellow]no session — planning a fresh squad[/yellow]"
        if config.secrets.team_id:
            try:
                my_team = client.my_team(config.secrets.team_id)
                auth_note = "[green]authenticated[/green]"
            except AuthRequired:
                auth_note = "[red]session rejected — run `arsenal auth attach`[/red]"

    if nxt is None:
        console.print("[yellow]no upcoming gameweek — the season may be over[/yellow]")
        raise typer.Exit(0)

    remaining = nxt.deadline_time - started
    hours = remaining.total_seconds() / 3600
    console.print(
        f"GW{nxt.id} deadline {nxt.deadline_time:%a %d %b %H:%M UTC} "
        f"([cyan]T−{hours:.1f}h[/cyan]) · {auth_note}"
    )
    console.print(
        f"{len(history.gameweeks)} completed gameweeks · "
        f"{len(bootstrap.elements)} players · {len(fixtures)} fixtures"
    )

    if not history.gameweeks:
        console.print("[yellow]no completed gameweeks — nothing to forecast from[/yellow]")
        raise typer.Exit(0)

    # ------------------------------------------------------------------ research
    console.rule("[bold]research")
    from .cli import _gather_evidence  # imported late to avoid a circular import

    _, evidence, notes = _gather_evidence(
        config, bootstrap, use_llm=use_llm, quiet=False, raw=raw_elements
    )
    for note in notes:
        console.print(f"  {note}")
    if not use_llm:
        console.print(
            "  [dim]claim extraction off — set ANTHROPIC_API_KEY or "
            "GEMINI_API_KEY to read press conferences and community "
            "sources[/dim]"
        )

    summary = summarise_evidence(evidence)
    console.print(
        f"\n{summary.get('total', 0)} records · {summary.get('actionable', 0)} actionable "
        f"(T1 {summary.get('tier1', 0)} · T2 {summary.get('tier2', 0)} · "
        f"T3 {summary.get('tier3', 0)} · T4 {summary.get('tier4', 0)})"
    )

    # ------------------------------------------------------------------ forecast
    console.rule("[bold]forecast")
    steps = horizon or settings.horizon
    league = build_league_model(history, bootstrap)
    schedule = upcoming_fixtures(fixtures, start_gameweek=nxt.id, horizon=steps)
    forecasts = forecast_players(bootstrap, history, league, schedule, horizon=steps)

    report = build_report(
        evidence,
        gameweek=nxt.id,
        max_tier=Tier(config.research.max_tier_that_moves_forecast),
    )
    apply_to_forecasts(forecasts, report)
    console.print(
        f"forecast {len(forecasts)} players over {steps} gameweeks · "
        f"{len(report.changed_players)} adjusted by team news"
    )
    if report.conflicts:
        console.print(
            f"[yellow]{len(report.conflicts)} source conflicts[/yellow] — uncertainty widened"
        )
    if report.hypotheses:
        console.print(
            f"[dim]{len(report.hypotheses)} Tier 4 claims held as "
            "hypotheses (they moved nothing)[/dim]"
        )

    # ------------------------------------------------------------------ optimise
    console.rule("[bold]optimise")
    if my_team is None:
        console.print(
            "[yellow]cannot plan transfers without your squad.[/yellow] "
            "Run `arsenal auth attach`, then try again."
        )
        raise typer.Exit(1)

    opt = OptimiserConfig(
        horizon=steps,
        discount=settings.discount,
        risk_aversion=settings.risk_aversion,
        bench_weight=settings.bench_weight,
        max_hit=settings.max_hit,
        hit_margin=settings.hit_margin,
        max_free_transfers=bootstrap.game_config.rules.max_free_transfers,
        solver_time_limit=settings.solver_time_limit,
        squad_requirements=bootstrap.squad_requirements(),
        play_limits=bootstrap.play_limits(),
    )
    candidates = candidates_from_forecasts(bootstrap, forecasts, my_team=my_team)
    free = my_team.transfers.limit or 1

    try:
        plan = optimise(
            candidates,
            initial_bank=my_team.transfers.bank,
            initial_free_transfers=free,
            config=opt,
            chip=chip,
        )
    except OptimiserError as exc:
        console.print(f"[red]optimiser failed[/red]: {exc}")
        raise typer.Exit(1) from exc

    decision = plan.this_week
    index = {c.element_id: c for c in candidates}
    console.print(
        f"solved over {len(candidates)} candidates · {free} free transfer(s) · "
        f"bank {format_money(my_team.transfers.bank)}"
    )

    # ------------------------------------------------------------------ validate
    console.rule("[bold]validate")
    bench_rank = {pid: rank + 1 for rank, pid in enumerate(decision.bench)}
    squad = [
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
    spent = sum(p.sells_for for p in squad)
    validation = validate_squad(
        squad,
        budget=spent + decision.bank,
        squad_requirements=bootstrap.squad_requirements(),
        play_limits=bootstrap.play_limits(),
        elements=elements,
    )
    if validation.ok:
        console.print("[green]passed[/green] — every rule invariant satisfied")
    else:
        console.print(f"[red]FAILED[/red]\n{validation}")
        console.print("\n[red]The agent would abort here rather than submit.[/red]")
        raise typer.Exit(2)
    for warning in validation.warnings:
        console.print(f"[yellow]warn[/yellow] {warning}")

    # -------------------------------------------------------------- the summary
    console.rule("[bold]what the agent would do")
    _summary(
        decision=decision,
        plan=plan,
        index=index,
        elements=elements,
        teams=teams,
        forecasts=forecasts,
        bootstrap=bootstrap,
        my_team=my_team,
        gameweek=nxt.id,
        deadline_hours=hours,
        horizon=steps,
        hit_margin=settings.hit_margin,
    )

    _drivers(
        decision=decision,
        report=report,
        elements=elements,
        teams=teams,
        forecasts=forecasts,
        owned={pick.element for pick in my_team.picks},
    )

    console.print(
        f"\n[bold green]DRY RUN[/bold green] — nothing was submitted. "
        f"Completed in {(datetime.now(UTC) - started).total_seconds():.0f}s."
    )


def _summary(
    *,
    decision,
    plan,
    index,
    elements,
    teams,
    forecasts,
    bootstrap,
    my_team,
    gameweek: int,
    deadline_hours: float,
    horizon: int,
    hit_margin: float,
) -> None:
    """The notification, near enough verbatim."""

    def name(element_id: int) -> str:
        element = elements.get(element_id)
        if element is None:
            return str(element_id)
        club = teams[element.team].short_name if element.team in teams else "?"
        return f"{element.name} ({club})"

    lines: list[str] = []

    if decision.transfers_in:
        outgoing = sorted(decision.transfers_out, key=lambda i: sum(index[i].xp), reverse=True)
        incoming = sorted(decision.transfers_in, key=lambda i: sum(index[i].xp), reverse=True)
        for out_id, in_id in zip(outgoing, incoming, strict=False):
            gain = sum(index[in_id].xp) - sum(index[out_id].xp)
            lines.append(f"  OUT {name(out_id):28} IN {name(in_id):28} {gain:+.1f} xP")
        cost = decision.hit_cost
        lines.append(
            f"\n  {len(decision.transfers_in)} transfer(s)"
            + (
                f", taking a [red]-{cost}[/red] hit (cleared the {hit_margin:.0f}-point margin)"
                if cost
                else " — all free"
            )
        )
    else:
        lines.append("  [dim]No transfers. Nothing clears the cost of making one.[/dim]")

    console.print(Panel("\n".join(lines), title="Transfers", title_align="left"))

    xi = Table(
        "pos",
        Column("player", min_width=18, no_wrap=True),
        "price",
        "xP",
        title=f"GW{gameweek} starting XI",
        title_justify="left",
    )
    for element_id in sorted(decision.starting, key=lambda i: index[i].position):
        element = elements[element_id]
        label = name(element_id)
        if element_id == decision.captain:
            label = f"[bold]{label} (C)[/bold]"
        elif element_id == decision.vice_captain:
            label = f"{label} (V)"
        xi.add_row(
            index[element_id].position.short,
            label,
            format_money(element.now_cost),
            f"{index[element_id].points(0):.2f}",
        )
    console.print(xi)

    bench = " · ".join(
        f"{n}. {name(pid)} {index[pid].points(0):.1f}"
        for n, pid in enumerate(decision.bench, start=1)
    )
    console.print(f"[dim]Bench: {bench}[/dim]")

    captain = forecasts.get(decision.captain)
    if captain:
        rivals = sorted(
            (forecasts[i] for i in decision.starting if i in forecasts),
            key=lambda f: f.xp[0],
            reverse=True,
        )[:2]
        note = ""
        if len(rivals) == 2:
            gap = rivals[0].xp[0] - rivals[1].xp[0]
            if gap < rivals[0].sigma[0] / 2:
                note = (
                    f" — but only {gap:.2f} ahead of {rivals[1].name}, "
                    f"well inside the ±{rivals[0].sigma[0]:.1f} uncertainty"
                )
        console.print(
            f"\n[bold]Captain[/bold] {captain.name} at {captain.xp[0]:.2f} xP, "
            f"doubled to {captain.xp[0] * 2:.2f}{note}"
        )

    used = [
        str(c.get("name", "")) for c in my_team.chips if c.get("status_for_entry") == "played"
    ]
    remaining_chips = available_chips(bootstrap, used, gameweek)
    console.print(
        f"[bold]Chip[/bold] {plan.chip or 'none'} · "
        f"available: {', '.join(remaining_chips) or 'none'}"
    )

    console.print(
        f"\n[bold]Expected[/bold] {decision.expected_points:.1f} points this gameweek, "
        f"{plan.total_expected_points:.1f} over {horizon} · "
        f"bank {format_money(decision.bank)} · "
        f"submitting at T−{max(deadline_hours - 1.5, 0):.0f}h"
    )


def _drivers(*, decision, report, elements, teams, forecasts, owned: set[int]) -> None:
    """What actually moved the answer, separated from what merely happened.

    431 evidence records adjusted 201 players, but only a handful bear on the
    squad that was chosen. Reporting all of them is noise; reporting none leaves
    a recommendation you cannot interrogate. This shows the evidence attached to
    players entering, leaving, or starting — and says plainly when a decision
    rested on statistics alone, because "no team news" is itself worth knowing.
    """

    def name(element_id: int) -> str:
        element = elements.get(element_id)
        if element is None:
            return str(element_id)
        club = teams[element.team].short_name if element.team in teams else "?"
        return f"{element.name} ({club})"

    console.rule("[bold]why")

    relevant = set(decision.transfers_in) | set(decision.transfers_out) | set(decision.starting)
    moved = [
        adjustment
        for player_id, adjustment in report.adjustments.items()
        if player_id in relevant and adjustment.changed
    ]

    # Transfers first — these are the decisions that cost something.
    for label, players in (
        ("[red]OUT[/red]", decision.transfers_out),
        ("[green]IN[/green]", decision.transfers_in),
    ):
        for player_id in players:
            adjustment = report.adjustments.get(player_id)
            console.print(f"\n  {label} [bold]{name(player_id)}[/bold]")
            if adjustment and adjustment.reasons:
                for reason in adjustment.reasons[:3]:
                    console.print(f"      {reason}")
            else:
                # An honest and common answer. Most transfers are driven by the
                # forecast, not by news, and pretending otherwise would invent a
                # narrative the model never used.
                console.print(
                    "      [dim]no team news — chosen on form, fixtures and "
                    "expected minutes[/dim]"
                )

    # Then anyone in the XI whose forecast the news changed.
    starters_moved = [
        adjustment
        for adjustment in moved
        if adjustment.player_id in decision.starting
        and adjustment.player_id not in decision.transfers_in
    ]
    if starters_moved:
        console.print("\n  [bold]team news affecting your XI[/bold]")
        for adjustment in sorted(starters_moved, key=lambda a: a.availability_multiplier)[:5]:
            console.print(f"    {name(adjustment.player_id)}")
            for reason in adjustment.reasons[:2]:
                console.print(f"      {reason}")

    # Evidence that downgraded someone you own but did not act on — the most
    # decision-relevant thing the agent can surface, because it is the case where
    # it might be wrong to hold.
    held_down = [
        adjustment
        for player_id, adjustment in report.adjustments.items()
        if player_id in owned
        and player_id not in decision.transfers_out
        and adjustment.availability_multiplier < 0.7
    ]
    if held_down:
        console.print("\n  [yellow]owned but flagged, and not transferred[/yellow]")
        for adjustment in sorted(held_down, key=lambda a: a.availability_multiplier)[:5]:
            console.print(
                f"    {name(adjustment.player_id)} "
                f"[dim]availability x{adjustment.availability_multiplier:.2f}[/dim]"
            )
            for reason in adjustment.reasons[:1]:
                console.print(f"      {reason}")

    if report.conflicts:
        affected = {
            first.player_id for first, _ in report.conflicts if first.player_id in relevant
        }
        if affected:
            plural = "player has" if len(affected) == 1 else "players have"
            console.print(
                f"\n  [yellow]{len(affected)} of your {plural} sources that "
                "disagree[/yellow] — uncertainty widened rather than a side picked"
            )
            for player_id in list(affected)[:3]:
                console.print(f"    {name(player_id)}")

    if not moved and not held_down:
        console.print(
            "\n  [dim]No team news changed any player in this squad. The "
            "decision rests entirely on the statistical forecast.[/dim]"
        )

"""``arsenal explain`` — why the agent picked what it picked.

A squad you cannot interrogate is a squad you cannot trust, and an
auto-submitting agent has to earn trust before it earns autonomy. This command
shows the working: where each player's expected points come from, what each
transfer actually buys, and whether a hit clears its cost by enough to be worth
the certainty it gives up.

It is deliberately sceptical. Where a number rests on thin evidence it says so,
because the failure mode that matters is not being wrong — it is being wrong
confidently.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Column, Table

from .config import Config
from .forecast import build_league_model, forecast_players, load_history, upcoming_fixtures
from .forecast.model import PlayerForecast
from .fpl.client import AuthRequired
from .money import format_money
from .optimizer import OptimiserConfig, OptimiserError, candidates_from_forecasts, optimise
from .research import apply_to_forecasts, build_report
from .session import authenticated_client

console = Console()

# Below this many minutes, a per-90 rate is mostly noise and any forecast built
# on it should be read with suspicion rather than confidence.
THIN_EVIDENCE_MINUTES = 270


def _component_table(forecast: PlayerForecast, title: str) -> Table:
    """Break one player's expected points into where they come from."""
    breakdown = forecast.breakdowns[0]
    table = Table("component", "xP", "why", title=title, title_justify="left")
    minutes = forecast.minutes[0]

    rows = [
        (
            "appearance",
            breakdown.appearance,
            f"{minutes.p_appear:.0%} to play, {minutes.p_sixty:.0%} to reach 60 min",
        ),
        (
            "goals",
            breakdown.goals,
            f"{forecast.rates.xg90:.2f} xG/90" if forecast.rates else "",
        ),
        (
            "assists",
            breakdown.assists,
            f"{forecast.rates.xa90:.2f} xA/90" if forecast.rates else "",
        ),
        ("clean sheet", breakdown.clean_sheet, "opponent strength × P(60 min)"),
        ("conceding", breakdown.goals_conceded, "−1 per 2 conceded, as a step function"),
        (
            "defensive contribution",
            breakdown.defensive_contribution,
            f"{forecast.rates.p_dc:.0%} to cross the action threshold"
            if forecast.rates
            else "",
        ),
        ("saves", breakdown.saves, "saves/90 ÷ 3, scaled by opponent threat"),
        ("bonus", breakdown.bonus, "historical bonus rate per 90"),
        ("cards", breakdown.cards, "yellow card rate"),
    ]
    for name, value, why in rows:
        if abs(value) < 0.005:
            continue
        table.add_row(name, f"{value:+.2f}", why)
    table.add_row("[bold]total[/bold]", f"[bold]{breakdown.total:.2f}[/bold]", "")
    return table


def explain(
    horizon: Annotated[int, typer.Option("--horizon", help="Gameweeks to plan over")] = 0,
    player: Annotated[
        str | None, typer.Option("--player", help="Explain one player's forecast")
    ] = None,
) -> None:
    """Show the reasoning behind the current plan.

    With ``--player``, breaks one player's expected points into components
    instead. Useful when a forecast looks wrong and you want to see which part
    of it is driving the number.
    """
    config = Config.load()
    settings = config.optimiser

    with authenticated_client(config, required=False) as client:
        bootstrap = client.bootstrap()
        elements = bootstrap.element_by_id()
        teams = bootstrap.team_by_id()
        history = load_history(client, bootstrap)
        fixtures = client.fixtures()
        nxt = bootstrap.next_event
        try:
            my_team = client.my_team(config.secrets.team_id) if config.secrets.team_id else None
        except AuthRequired:
            my_team = None

    if not history.gameweeks:
        console.print("[yellow]no completed gameweeks yet — nothing to explain[/yellow]")
        raise typer.Exit(0)

    steps = horizon or settings.horizon
    start = nxt.id if nxt else history.latest_gameweek + 1
    league = build_league_model(history, bootstrap)
    schedule = upcoming_fixtures(fixtures, start_gameweek=start, horizon=steps)
    forecasts = forecast_players(bootstrap, history, league, schedule, horizon=steps)
    apply_to_forecasts(forecasts, build_report([]))

    by_name = {f.name.lower(): f for f in forecasts.values()}

    # ---------------------------------------------------------------- one player
    if player:
        match = by_name.get(player.lower()) or next(
            (f for name, f in by_name.items() if player.lower() in name), None
        )
        if match is None:
            console.print(f"[red]no player matching '{player}'[/red]")
            raise typer.Exit(1)

        element = elements[match.element_id]
        club = teams[match.team].short_name if match.team in teams else "?"
        console.print(
            f"\n[bold]{match.name}[/bold] ({club}, {match.position.short}, "
            f"{format_money(element.now_cost)})\n"
        )
        console.print(_component_table(match, f"GW{start} expected points"))

        if match.rates and match.rates.minutes < THIN_EVIDENCE_MINUTES:
            console.print(
                f"\n[yellow]thin evidence[/yellow]: only {match.rates.minutes:.0f} minutes "
                "played, so these rates are shrunk heavily toward a positional prior. "
                "Treat the number as a weak signal."
            )
        sigma = match.sigma[0]
        console.print(
            f"\nuncertainty ±{sigma:.1f} — roughly "
            f"{max(0.0, match.xp[0] - sigma):.1f} to {match.xp[0] + sigma:.1f} points"
        )
        raise typer.Exit(0)

    # ------------------------------------------------------------- the whole plan
    if my_team is None:
        console.print(
            "[yellow]no session[/yellow] — cannot explain transfers without your squad.\n"
            "Run `arsenal auth attach`, or use `--player <name>` to inspect a forecast."
        )
        raise typer.Exit(1)

    candidates = candidates_from_forecasts(bootstrap, forecasts, my_team=my_team)
    base = OptimiserConfig(
        horizon=steps,
        discount=settings.discount,
        risk_aversion=settings.risk_aversion,
        bench_weight=settings.bench_weight,
        max_hit=settings.max_hit,
        hit_margin=settings.hit_margin,
        max_free_transfers=bootstrap.game_config.rules.max_free_transfers,
        squad_requirements=bootstrap.squad_requirements(),
        play_limits=bootstrap.play_limits(),
    )
    free = my_team.transfers.limit or 1
    bank = my_team.transfers.bank

    # Solve at each transfer count so the *marginal* value of each one is visible.
    # A total is not an argument; the question for every transfer past the free
    # ones is whether that specific transfer beats the -4 it costs.
    console.print("\n[bold]What each transfer is worth[/bold]\n")
    ladder = Table(
        "transfers", "hit", "xP this GW", f"xP over {steps} GW", "marginal", "verdict"
    )
    previous: float | None = None
    for n in range(0, min(free + 2, 5) + 1):
        try:
            plan = optimise(
                candidates,
                initial_bank=bank,
                initial_free_transfers=free,
                config=base,
                max_transfers=n,
            )
        except OptimiserError:
            continue

        total = plan.total_expected_points
        hit = plan.this_week.hit_cost
        marginal = "—" if previous is None else f"{total - previous:+.1f}"

        if n <= free:
            verdict = "[green]free[/green]"
        elif previous is None:
            verdict = ""
        else:
            gain = total - previous
            if gain > hit + settings.hit_margin:
                verdict = "[green]clears the margin[/green]"
            elif gain > hit:
                verdict = "[yellow]beats -4, but not by enough[/yellow]"
            else:
                verdict = "[red]does not pay for itself[/red]"

        ladder.add_row(
            str(n),
            f"-{hit}" if hit else "0",
            f"{plan.this_week.expected_points:.1f}",
            f"{total:.1f}",
            marginal,
            verdict,
        )
        previous = total
    console.print(ladder)
    console.print(
        f"[dim]A hit must gain more than {base.hit_margin:.0f} points beyond its -4 "
        "before it is taken. The -4 is certain; the gain is a forecast.[/dim]"
    )

    # --------------------------------------------------------- the chosen plan
    plan = optimise(candidates, initial_bank=bank, initial_free_transfers=free, config=base)
    decision = plan.this_week
    index = {c.element_id: c for c in candidates}

    if decision.transfers_in:
        console.print("\n[bold]Proposed transfers[/bold]\n")
        swaps = Table(
            Column("out", min_width=13),
            "xP",
            Column("in", min_width=13),
            "xP",
            "gain",
            "note",
        )
        outgoing = sorted(decision.transfers_out, key=lambda i: sum(index[i].xp), reverse=True)
        incoming = sorted(decision.transfers_in, key=lambda i: sum(index[i].xp), reverse=True)
        for out_id, in_id in zip(outgoing, incoming, strict=False):
            out_xp, in_xp = sum(index[out_id].xp), sum(index[in_id].xp)
            out_f, in_f = forecasts.get(out_id), forecasts.get(in_id)
            note = ""
            if in_f and in_f.rates and in_f.rates.minutes < THIN_EVIDENCE_MINUTES:
                note = "[yellow]thin evidence on the incoming player[/yellow]"
            elif out_f and out_f.minutes[0].p_appear < 0.5:
                note = "outgoing player is unlikely to play"
            swaps.add_row(
                index[out_id].name,
                f"{out_xp:.1f}",
                index[in_id].name,
                f"{in_xp:.1f}",
                f"{in_xp - out_xp:+.1f}",
                note,
            )
        console.print(swaps)
    else:
        console.print("\n[dim]no transfers proposed — nothing clears its cost[/dim]")

    # ------------------------------------------------------------------ captain
    captain = forecasts.get(decision.captain)
    if captain:
        alternatives = sorted(
            (forecasts[i] for i in decision.starting if i in forecasts),
            key=lambda f: f.xp[0],
            reverse=True,
        )[:3]
        console.print("\n[bold]Captaincy[/bold]\n")
        table = Table("player", "xP", "doubled", "uncertainty")
        for option in alternatives:
            marker = " (C)" if option.element_id == decision.captain else ""
            table.add_row(
                f"{option.name}{marker}",
                f"{option.xp[0]:.2f}",
                f"{option.xp[0] * 2:.2f}",
                f"±{option.sigma[0]:.1f}",
            )
        console.print(table)
        console.print(
            "[dim]The captain is simply the highest expected scorer in the XI. "
            "Uncertainty is shown because a narrow gap is not a real gap.[/dim]"
        )

    console.print(
        f"\n[dim]Forecast built from {len(history.gameweeks)} completed gameweeks. "
        "Early in a season, rates are shrunk hard toward positional priors, so "
        "differences between similar players are weak evidence.[/dim]"
    )

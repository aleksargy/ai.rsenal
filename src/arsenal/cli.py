"""``arsenal`` command line.

``doctor`` first — it probes every read endpoint and reports API drift, which is
the fastest way to tell whether a failure is your code or the season having moved
underneath it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from datetime import UTC, datetime
from itertools import zip_longest
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Column, Table

from .cli_auth import auth_app
from .cli_channels import channels as channels_command
from .cli_explain import explain as explain_command
from .cli_run import run as run_command
from .cli_sources import sources as sources_command
from .config import Config
from .forecast import build_league_model, forecast_players, load_history, upcoming_fixtures
from .forecast.backtest import backtest as run_backtest
from .fpl.client import AuthRequired, FPLClient, FPLError
from .fpl.rules import SquadPlayer, available_chips, valid_formations, validate_squad
from .money import format_money
from .optimizer import (
    OptimiserConfig,
    OptimiserError,
    build_candidates,
    candidates_from_forecasts,
    optimise,
)
from .research import (
    FPLNewsSource,
    PlayerResolver,
    ScoutRiskSource,
    SetPieceSource,
    Tier,
    apply_to_forecasts,
    build_client,
    build_report,
    club_article_links,
    deduplicate,
    extract_claims,
    fetch_club_articles,
    fetch_news_documents,
    fetch_reddit_documents,
    fetch_youtube_documents,
    summarise_evidence,
)
from .session import authenticated_client

# Windows consoles default to cp1252, which cannot encode several characters
# this CLI prints — the typographic minus in "T−72h", the middot separators, the
# em dashes. Without this, `arsenal doctor` dies with a UnicodeEncodeError on a
# stock PowerShell, which looks like a crash in the tool rather than a terminal
# limitation. `errors="replace"` means an unrenderable glyph degrades to a
# placeholder instead of taking the command down.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

app = typer.Typer(
    name="arsenal",
    help="Autonomous Fantasy Premier League manager.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
app.add_typer(auth_app, name="auth")
app.command("explain")(explain_command)
app.command("run")(run_command)
app.command("sources")(sources_command)
app.command("channels")(channels_command)


def _client(config: Config, *, gameweek: int | None = None) -> FPLClient:
    """A client that authenticates when it can and reads publicly when it cannot.

    `required=False` because every read endpoint works without a session — a
    missing credential should cost you your own squad, not the whole CLI.
    """
    return authenticated_client(config, gameweek=gameweek, required=False)


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
    research: Annotated[
        bool, typer.Option("--research/--no-research", help="Apply team news")
    ] = True,
    llm: Annotated[
        bool, typer.Option("--llm/--no-llm", help="Extract claims with Claude")
    ] = False,
) -> None:
    """Solve for the best squad and print the proposed gameweek.

    Expected points come from the bottom-up forecast: appearance and 60-minute
    probabilities, shrunk attacking rates adjusted for the opponent, clean-sheet
    probability from a Poisson model, and defensive contribution as a threshold
    crossing. Run `arsenal backtest` to see how it scores against completed
    gameweeks.
    """
    config = Config.load()
    settings = config.optimiser

    with _client(config) as client:
        bootstrap = client.bootstrap()
        elements = bootstrap.element_by_id()
        teams = bootstrap.team_by_id()
        nxt = bootstrap.next_event
        history = load_history(client, bootstrap)
        all_fixtures = client.fixtures()
        raw_elements = client.raw_elements()

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
        hit_margin=settings.hit_margin,
        max_free_transfers=bootstrap.game_config.rules.max_free_transfers,
        solver_time_limit=settings.solver_time_limit,
        squad_requirements=bootstrap.squad_requirements(),
        play_limits=bootstrap.play_limits(),
    )

    start = nxt.id if nxt else history.latest_gameweek + 1
    if history.gameweeks:
        league = build_league_model(history, bootstrap)
        schedule = upcoming_fixtures(
            all_fixtures, start_gameweek=start, horizon=opt_config.horizon
        )
        forecasts = forecast_players(
            bootstrap, history, league, schedule, horizon=opt_config.horizon
        )
        if research:
            _, evidence, notes = _gather_evidence(
                config, bootstrap, use_llm=llm, quiet=True, raw=raw_elements
            )
            report = build_report(
                evidence,
                gameweek=start,
                max_tier=Tier(config.research.max_tier_that_moves_forecast),
            )
            apply_to_forecasts(forecasts, report)
            changed = len(report.changed_players)
            for note in notes:
                console.print(f"  {note}")
            console.print(f"  [green]research[/green]: {changed} players adjusted\n")

        candidates = candidates_from_forecasts(bootstrap, forecasts, my_team=my_team)
        source = f"{len(history.gameweeks)} completed GW"
    else:
        # Pre-season, or a fresh dataset: no match history to build rates from.
        candidates = build_candidates(bootstrap, my_team=my_team, horizon=opt_config.horizon)
        source = "no history - placeholder forecast"

    console.print(
        f"[dim]pool: {len(candidates)} players · horizon {opt_config.horizon} GW · "
        f"forecast from {source} · "
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
        "\n[dim]Tier 1 team news is applied. Press conferences and community "
        "signal need --llm and an Anthropic key; Tier 4 never moves a number on "
        "its own. Run `arsenal backtest` to see how the model scores.[/dim]"
    )


def _gather_evidence(config, bootstrap, *, use_llm: bool, quiet: bool = False, raw=None):
    """Collect evidence from every configured source, reporting what failed.

    Sources degrade to empty rather than raising: one dead scraper must not cost
    a gameweek. What was lost is returned so the caller can say so.
    """
    resolver = PlayerResolver.from_bootstrap(bootstrap)
    raw = raw or []
    evidence = []
    notes: list[str] = []

    for source in (
        FPLNewsSource(bootstrap),
        SetPieceSource(bootstrap),
        ScoutRiskSource(bootstrap, raw),
    ):
        result = source.safe_gather(resolver)
        if result.ok:
            evidence.extend(result.evidence)
            notes.append(f"[green]{result.name}[/green]: {len(result.evidence)} records")
        else:
            notes.append(f"[red]{result.name} failed[/red]: {result.error}")

    if not use_llm:
        return resolver, deduplicate(evidence), notes

    backend = build_client(
        config.secrets.anthropic_api_key,
        provider=config.research.provider,
        model=config.research.model,
        gemini_key=config.secrets.gemini_api_key,
    )
    if backend is None:
        notes.append(
            "[yellow]no model credentials[/yellow] — skipping claim extraction. "
            "Set ANTHROPIC_API_KEY or GEMINI_API_KEY. Tier 1 sources still applied."
        )
        return resolver, deduplicate(evidence), notes

    # Official club articles first — FPL links the manager's own press conference
    # on the club's own site, which is the best team-news source available and
    # needs no third-party scraping.
    teams = {t.id: t.short_name for t in bootstrap.teams}
    articles = club_article_links(raw, teams)
    documents, article_problems = fetch_club_articles(articles)
    if documents:
        notes.append(
            f"[green]club news[/green]: {len(documents)} official articles "
            f"from {len(articles)} links"
        )
    if article_problems:
        notes.append(f"[yellow]{len(article_problems)} club articles unreachable[/yellow]")

    # Free, keyless, and Tier 3 — named outlets carrying named journalists.
    news_docs, news_problems = fetch_news_documents()
    if news_docs:
        notes.append(f"[green]news feeds[/green]: {len(news_docs)} relevant items")
    for problem in news_problems:
        notes.append(f"[yellow]feed unavailable[/yellow]: {problem}")
    documents.extend(news_docs)

    # Reddit is optional and Tier 4: it cannot move a forecast at the default
    # threshold, and API access now sits behind a Responsible Builder Policy.
    if config.secrets.reddit_client_id and config.secrets.reddit_client_secret:
        reddit_docs, reddit_error = fetch_reddit_documents(
            config.research.subreddits,
            client_id=config.secrets.reddit_client_id,
            client_secret=config.secrets.reddit_client_secret,
        )
        if reddit_error:
            notes.append(f"[yellow]reddit skipped[/yellow]: {reddit_error}")
        else:
            notes.append(f"[green]reddit[/green]: {len(reddit_docs)} documents")
        documents.extend(reddit_docs)

    videos, youtube_error = fetch_youtube_documents(
        config.secrets.youtube_api_key, config.research.youtube_channels
    )
    if youtube_error:
        notes.append(f"[yellow]youtube degraded[/yellow]: {youtube_error}")
    if videos:
        notes.append(f"[green]youtube[/green]: {len(videos)} documents")
    documents.extend(videos)

    if documents:
        batch_size = 8
        requests = max(1, -(-len(documents) // batch_size))
        if not quiet:
            note = f"[dim]extracting from {len(documents)} documents in {requests} request(s)"
            if backend.name == "gemini":
                note += " — free tier is 5/min and 20/day, so this is paced"
            console.print(note + "[/dim]")

        def report(done: int, total: int, claims: int) -> None:
            if not quiet:
                console.print(f"  [dim]batch {done}/{total} · {claims} claims so far[/dim]")

        extracted, problems = extract_claims(
            backend,
            documents,
            resolver,
            batch_size=batch_size,
            on_progress=report,
        )
        evidence.extend(extracted)
        notes.append(
            f"[green]extraction[/green] ({backend.name}/{backend.model}): "
            f"{len(extracted)} claims"
        )
        if problems:
            notes.append(
                f"[yellow]{len(problems)} claims dropped[/yellow] (unresolved or invalid)"
            )

    return resolver, deduplicate(evidence), notes


@app.command()
def research(
    llm: Annotated[
        bool, typer.Option("--llm/--no-llm", help="Extract claims with Claude")
    ] = True,
    top: Annotated[int, typer.Option("--top", help="Adjustments to show")] = 20,
) -> None:
    """Gather team news and show how it would change the forecast.

    Tier 1-3 evidence can move a number. Tier 4 (creators, Reddit opinion) never
    does on its own — it surfaces claims to verify and reads what the field is
    doing. That rule is enforced in code, not left to judgement.
    """
    config = Config.load()
    with _client(config) as client:
        bootstrap = client.bootstrap()
        raw_elements = client.raw_elements()

    nxt = bootstrap.next_event
    gameweek = nxt.id if nxt else None
    max_tier = Tier(config.research.max_tier_that_moves_forecast)

    resolver, evidence, notes = _gather_evidence(
        config, bootstrap, use_llm=llm, raw=raw_elements
    )
    for note in notes:
        console.print(f"  {note}")

    summary = summarise_evidence(evidence)
    console.print(
        f"\n[bold]{summary.get('total', 0)}[/bold] records · "
        f"[bold]{summary.get('actionable', 0)}[/bold] actionable "
        f"(T1 {summary.get('tier1', 0)} · T2 {summary.get('tier2', 0)} · "
        f"T3 {summary.get('tier3', 0)} · T4 {summary.get('tier4', 0)})"
    )

    report = build_report(evidence, gameweek=gameweek, max_tier=max_tier)
    changed = sorted(report.changed_players, key=lambda a: a.availability_multiplier)[:top]

    if changed:
        table = Table(
            Column("player", min_width=14, no_wrap=True),
            "avail",
            "why",
            title="Forecast adjustments",
        )
        for adjustment in changed:
            table.add_row(
                resolver.describe(adjustment.player_id),
                f"x{adjustment.availability_multiplier:.2f}",
                adjustment.reasons[0][:74] if adjustment.reasons else "",
            )
        console.print(table)
    else:
        console.print("[dim]no evidence changed any forecast[/dim]")

    if report.conflicts:
        console.print(
            f"\n[yellow]{len(report.conflicts)} conflicts[/yellow] — "
            "sources disagree; uncertainty widened"
        )
    if report.hypotheses:
        console.print(
            f"[dim]{len(report.hypotheses)} Tier 4 claims recorded as hypotheses. "
            "These moved nothing — verify at a higher tier before acting.[/dim]"
        )
    if report.stale_dropped:
        console.print(f"[dim]{report.stale_dropped} stale claims discarded[/dim]")
    if report.out_of_scope:
        console.print(f"[dim]{report.out_of_scope} claims apply to a different gameweek[/dim]")


@app.command()
def backtest(
    first: Annotated[int | None, typer.Option("--from", help="First gameweek")] = None,
    last: Annotated[int | None, typer.Option("--to", help="Last gameweek")] = None,
) -> None:
    """Score the forecast against completed gameweeks.

    Each gameweek is predicted using only data from strictly before it, with
    current injury status disabled — otherwise the model would be told who got
    injured, which it could not have known at the time.

    Rank correlation is the number that matters: squad selection needs players
    ordered correctly, not their totals predicted exactly.
    """
    config = Config.load()
    with _client(config) as client:
        bootstrap = client.bootstrap()
        results = run_backtest(client, bootstrap, first=first, last=last)

    if not results:
        console.print(
            "[yellow]not enough completed gameweeks to backtest[/yellow] — "
            "at least two are needed."
        )
        raise typer.Exit(0)

    table = Table(
        "GW",
        "n",
        "MAE",
        "baseline",
        "rank corr",
        "base corr",
        "top 10",
        "field",
        title="Backtest — predicting each gameweek from those before it",
    )
    for r in results:
        better = "[green]" if r.mae < r.baseline_mae else "[red]"
        table.add_row(
            str(r.gameweek),
            str(r.n),
            f"{better}{r.mae:.2f}[/]",
            f"{r.baseline_mae:.2f}",
            f"{r.spearman:+.3f}",
            f"{r.baseline_spearman:+.3f}",
            f"{r.top10_actual:.1f}",
            f"{r.field_actual:.1f}",
        )
    console.print(table)

    n = len(results)
    mae = sum(r.mae for r in results) / n
    base_mae = sum(r.baseline_mae for r in results) / n
    corr = sum(r.spearman for r in results) / n
    base_corr = sum(r.baseline_spearman for r in results) / n
    lift = sum(r.top10_actual for r in results) / n - sum(r.field_actual for r in results) / n

    console.print(
        f"\nmean MAE [bold]{mae:.3f}[/bold] vs baseline {base_mae:.3f} "
        f"({(base_mae - mae) / base_mae:+.1%})"
    )
    console.print(
        f"mean rank correlation [bold]{corr:+.3f}[/bold] vs baseline {base_corr:+.3f}"
    )
    console.print(
        f"top-10 picks outscored the field by [bold]{lift:+.2f}[/bold] points per gameweek"
    )
    console.print(
        "\n[dim]A handful of gameweeks is a small sample — treat these as "
        "directional. Baseline is each player's points per gameweek so far.[/dim]"
    )


@app.command()
def forecast(
    horizon: Annotated[int, typer.Option("--horizon", help="Gameweeks ahead")] = 1,
    top: Annotated[int, typer.Option("--top", help="Players to show")] = 20,
    position: Annotated[str | None, typer.Option("--position", help="GKP/DEF/MID/FWD")] = None,
) -> None:
    """Show the highest expected-points players, with their component breakdown."""
    config = Config.load()
    with _client(config) as client:
        bootstrap = client.bootstrap()
        history = load_history(client, bootstrap)
        fixtures = client.fixtures()

    if not history.gameweeks:
        console.print("[yellow]no completed gameweeks yet — nothing to forecast from[/yellow]")
        raise typer.Exit(0)

    nxt = bootstrap.next_event
    start = nxt.id if nxt else history.latest_gameweek + 1
    league = build_league_model(history, bootstrap)
    schedule = upcoming_fixtures(fixtures, start_gameweek=start, horizon=horizon)
    forecasts = forecast_players(bootstrap, history, league, schedule, horizon=horizon)

    teams = bootstrap.team_by_id()
    elements = bootstrap.element_by_id()
    wanted = position.upper() if position else None

    ranked = sorted(
        (f for f in forecasts.values() if not wanted or f.position.short == wanted),
        key=lambda f: sum(f.xp),
        reverse=True,
    )[:top]

    table = Table(
        "pos",
        Column("player", min_width=12, no_wrap=True),
        "club",
        "price",
        "p60",
        "xP",
        "gls",
        "ast",
        "CS",
        "DC",
        title=f"Expected points — GW{start}"
        + (f"-{start + horizon - 1}" if horizon > 1 else ""),
    )
    for f in ranked:
        element = elements[f.element_id]
        b = f.breakdowns[0]
        table.add_row(
            f.position.short,
            f.name,
            teams[f.team].short_name if f.team in teams else "?",
            format_money(element.now_cost),
            f"{f.minutes[0].p_sixty:.0%}",
            f"[bold]{sum(f.xp):.2f}[/bold]" if horizon > 1 else f"[bold]{b.total:.2f}[/bold]",
            f"{b.goals:.1f}",
            f"{b.assists:.1f}",
            f"{b.clean_sheet:.1f}",
            f"{b.defensive_contribution:.1f}",
        )
    console.print(table)
    console.print(
        f"\n[dim]From {len(history.gameweeks)} completed gameweeks. "
        "Rates are shrunk toward positional priors; defensive contribution is "
        "the probability of crossing the action threshold.[/dim]"
    )


if __name__ == "__main__":
    app()

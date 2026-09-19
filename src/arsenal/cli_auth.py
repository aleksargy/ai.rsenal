"""``arsenal auth`` — capturing and checking your FPL session.

Kept separate from the main CLI because session handling is the one part of this
system that needs regular human attention. Sessions expire, and when they do the
agent degrades to advisory mode and tells you to come here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console

from .config import Config, update_env_file
from .fpl.auth import (
    DEFAULT_CDP_ENDPOINT,
    Session,
    inspect_browser,
    login_with_browser,
    session_from_cdp,
    session_from_pasted_cookies,
)
from .fpl.client import AuthRequired, FPLClient, FPLError
from .fpl.oidc import OidcTokens, TokenError, ensure_fresh
from .money import format_money
from .session import SESSION_PATH, load_session, refresh_if_needed

auth_app = typer.Typer(
    help="Capture and check your FPL session.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _verify(session: Session, team_id: int | None) -> bool:
    """Prove a session works by actually reading privileged data with it.

    A cookie that *looks* right and a cookie that authenticates are different
    things, and only a live call to ``my-team/`` distinguishes them.
    """
    if not session.has_credentials:
        console.print("[red]nothing captured[/red]")
        return False

    console.print(f"[dim]{session.diagnose()}[/dim]")

    # Access tokens last an hour, so a stored one is usually dead by the time
    # anything checks it. Refreshing first is what makes unattended operation
    # possible at all.
    if session.tokens.needs_refresh and session.tokens.can_refresh:
        console.print("[dim]access token expired; refreshing...[/dim]")
        try:
            session.tokens, _ = ensure_fresh(session.tokens)
            console.print("[green]refreshed[/green] a new access token")
        except TokenError as exc:
            console.print(f"[yellow]refresh failed[/yellow]: {exc}")

    if team_id is None:
        console.print(
            "[yellow]FPL_TEAM_ID is not set[/yellow], so the session cannot be "
            "verified against your team yet. Set it, then run `arsenal auth check`."
        )
        return False

    # Try each credential shape in turn. FPL moved to OIDC this season and the
    # cookie names changed with no announcement, so which one works is an
    # empirical question — not something to assume from documentation that is
    # already out of date.
    attempts: list[tuple[str, dict[str, object]]] = []
    if session.cookies:
        attempts.append(("cookies", {"session_cookies": session.cookies}))
    if session.access_token:
        attempts.append(("bearer token", {"bearer_token": session.access_token}))
    if session.cookies and session.access_token:
        attempts.append(
            (
                "cookies + bearer token",
                {
                    "session_cookies": session.cookies,
                    "bearer_token": session.access_token,
                },
            )
        )

    config = Config.load()
    for label, kwargs in attempts:
        with FPLClient(config.cache_dir, **kwargs) as client:  # type: ignore[arg-type]
            try:
                team = client.my_team(team_id)
            except AuthRequired:
                console.print(f"  [yellow]{label}[/yellow]: rejected (403)")
                continue
            except FPLError as exc:
                console.print(f"  [yellow]{label}[/yellow]: {exc}")
                continue

        free = team.transfers.limit if team.transfers.limit is not None else "?"
        console.print(
            f"  [green]{label}[/green]: authenticated\n"
            f"[green]verified[/green] — read {len(team.picks)} picks, "
            f"bank {format_money(team.transfers.bank)}, {free} free transfers"
        )
        return True

    console.print("[red]none of the captured credentials authenticated.[/red]")
    return False


def _persist(session: Session, team_id: int | None, *, quiet: bool = False) -> None:
    """Save the session to disk and to .env, without ever printing it.

    An earlier version printed the value for manual pasting. That was wrong
    twice over: it put a live token into terminal scrollback, and the terminal
    wrapped the long JSON so pasting it back produced a multi-line value no
    ``KEY=value`` parser could read.
    """
    session.save(SESSION_PATH)
    env_path = update_env_file({"FPL_SESSION_JSON": session.to_env_value()})
    if quiet:
        return

    console.print(
        f"\nsaved to [bold]{SESSION_PATH.name}[/bold] and [bold]{env_path.name}[/bold]"
    )
    console.print("[dim]both are gitignored; the value is never printed[/dim]")

    expiry = session.token_expiry()
    if expiry is not None:
        remaining = expiry - datetime.now(UTC)
        minutes = remaining.total_seconds() / 60
        if minutes <= 0:
            console.print("[red]the OIDC token has already expired[/red]")
        else:
            console.print(
                f"[yellow]OIDC token expires in {minutes:.0f} minutes[/yellow] "
                f"({expiry:%H:%M UTC})"
            )

    if not team_id:
        console.print(
            "\n[yellow]FPL_TEAM_ID is still unset.[/yellow] Find it in the URL on your "
            "points page, then:\n"
            "  [dim]uv run arsenal auth whoami <id>[/dim]   to confirm it\n"
            "  [dim]uv run arsenal auth check[/dim]          to verify the session"
        )

    if session.tokens.refresh_token:
        console.print(
            "\n[bold]The agent now owns this login.[/bold] Refreshing advances a "
            "single shared chain, so from here:\n"
            "  · log out of FPL in that browser, and do not use it for FPL again\n"
            "  · do not re-run `attach` unless the agent's session actually breaks"
        )


@auth_app.command("login")
def auth_login(
    headless: Annotated[bool, typer.Option("--headless")] = False,
    timeout: Annotated[int, typer.Option("--timeout", help="Seconds to wait")] = 300,
) -> None:
    """Open a browser, wait for you to log in, and capture the session.

    Preferred over pasting cookies: it captures the whole session rather than the
    two cookies people usually remember, and the saved state replays directly
    into the browser executor layer.
    """
    config = Config.load()
    console.print("Opening a browser. Log in to FPL, then leave the window alone.\n")
    try:
        session = login_with_browser(timeout_seconds=timeout, headless=headless)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print("[green]captured[/green]")
    _verify(session, config.secrets.team_id)
    _persist(session, config.secrets.team_id)


@auth_app.command("attach")
def auth_attach(
    endpoint: Annotated[
        str, typer.Option("--endpoint", help="Chrome DevTools endpoint")
    ] = DEFAULT_CDP_ENDPOINT,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a session you already hold")
    ] = False,
) -> None:
    """Read the session from a Chrome you are already logged into.

    The most reliable option, and the only one that works with Google SSO —
    because nothing is automated except reading the result, so there is no login
    for Google to block.

    First close every Chrome window, then start it with remote debugging:

        chrome.exe --remote-debugging-port=9222

    Log into FPL in that window, then run this.
    """
    config = Config.load()

    # There is exactly one refresh-token chain per login, and refreshing it
    # advances that chain for everyone holding a copy. So when the agent already
    # has a live session, importing the browser's copy replaces a working token
    # with one the provider has already revoked — a capture that reports success
    # and then fails on the next refresh.
    existing = load_session(config)
    if (
        not force
        and existing is not None
        and existing.tokens.refresh_token
        and not existing.tokens.is_expired
    ):
        console.print(
            "[yellow]you already hold a live session — not overwriting it.[/yellow]\n\n"
            "Refreshing advances a single shared chain, so whichever side "
            "refreshed last invalidated the other's copy. Re-capturing now would "
            "import the browser's stale token and break a session that works.\n\n"
            "  · `arsenal auth check` to confirm what you have\n"
            "  · `--force` if the browser has genuinely signed in again since"
        )
        raise typer.Exit(0)

    try:
        session = session_from_cdp(endpoint)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print("[green]attached[/green]")
    _verify(session, config.secrets.team_id)
    _persist(session, config.secrets.team_id)


@auth_app.command("inspect")
def auth_inspect(
    endpoint: Annotated[
        str, typer.Option("--endpoint", help="Chrome DevTools endpoint")
    ] = DEFAULT_CDP_ENDPOINT,
) -> None:
    """Report what authentication state your logged-in browser actually holds.

    Use this when `attach` finds no session but you are definitely logged in.
    It enumerates cookies, localStorage and sessionStorage for premierleague.com
    so the real mechanism can be identified rather than guessed at.

    Values are redacted — only names, lengths and a short prefix are shown.
    """
    try:
        report = inspect_browser(endpoint)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    pages = [p for p in report["pages"] if "premierleague" in p]
    if pages:
        console.print("[dim]open Premier League pages:[/dim]")
        for page in pages:
            console.print(f"  {page}")
    else:
        console.print(
            "[yellow]no premierleague.com page is open in that browser[/yellow] — "
            "localStorage can only be read from an open page on the origin.\n"
            "Open https://fantasy.premierleague.com/my-team and run this again."
        )

    def dump(title: str, entries: dict) -> None:
        console.print(f"\n[bold]{title}[/bold] ({len(entries)})")
        if not entries:
            console.print("  [dim]none[/dim]")
            return
        for name, value in sorted(entries.items()):
            detail = value["value"] if isinstance(value, dict) else value
            suffix = ""
            if isinstance(value, dict) and value.get("http_only"):
                suffix = " [dim](httpOnly)[/dim]"
            console.print(f"  {name} = {detail}{suffix}")

    dump("cookies on premierleague.com", report["cookies"])
    dump("localStorage", report["local_storage"])
    dump("sessionStorage", report["session_storage"])

    if report["looks_like_jwt"]:
        console.print(
            f"\n[green]found what look like JWTs[/green]: {', '.join(report['looks_like_jwt'])}"
        )
        console.print(
            "[dim]That would mean FPL authenticates with a bearer token rather "
            "than a session cookie — a different replay mechanism entirely.[/dim]"
        )
    else:
        console.print("\n[dim]no JWT-shaped values found[/dim]")


@auth_app.command("paste")
def auth_paste() -> None:
    """Capture a session from cookies copied out of your browser.

    Use this when a browser cannot be driven. In DevTools: Application →
    Cookies → https://fantasy.premierleague.com, then copy the values of
    ``pl_profile`` and ``sessionid``.
    """
    console.print(
        "Paste your cookies, then press Enter on a blank line.\n"
        "[dim]Accepts 'pl_profile=...; sessionid=...' or one name/value per line.[/dim]\n"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)

    session = session_from_pasted_cookies("\n".join(lines))
    if not session.cookies:
        console.print("[red]nothing parsed[/red] — no cookies found in that input.")
        raise typer.Exit(1)

    config = Config.load()
    console.print(
        f"\n[green]parsed[/green] {len(session.cookies)} cookies: "
        + ", ".join(sorted(session.cookies))
    )
    _verify(session, config.secrets.team_id)
    _persist(session, config.secrets.team_id)


@auth_app.command("check")
def auth_check() -> None:
    """Check whether the configured session still works.

    Sessions expire — assume weeks, not months. This is the command to run when
    the agent reports it has degraded to advisory mode.
    """
    config = Config.load()
    if not config.secrets.team_id:
        console.print("[red]FPL_TEAM_ID is not set.[/red] Put it in .env first.")
        raise typer.Exit(1)

    # Loaded through the same path every other command uses, so `check` cannot
    # pass while real commands fail. An earlier version built the Session from
    # cookies alone and reported failure on a session that worked — it tested
    # the one credential FPL rejects and ignored the one it accepts.
    session = load_session(config)
    if session is None or not session.has_credentials:
        console.print("[yellow]no session found[/yellow] — run `arsenal auth attach`.")
        raise typer.Exit(1)
    source = "FPL_SESSION_JSON" if config.secrets.has_session else SESSION_PATH.name
    console.print(f"checking the session from [bold]{source}[/bold]...")

    if _verify(session, config.secrets.team_id):
        # The verify step may have refreshed. Persist it, or a rotated refresh
        # token is silently discarded and the next run has to start over.
        _persist(session, config.secrets.team_id, quiet=True)
    else:
        console.print(
            "\n[dim]Re-capture with `arsenal auth login`. Reads keep working "
            "without a session — only writes and your own squad need one.[/dim]"
        )
        raise typer.Exit(2)


@auth_app.command("whoami")
def auth_whoami(
    team_id: Annotated[int | None, typer.Argument(help="Team id to look up")] = None,
) -> None:
    """Show the public profile for a team id, to confirm you have the right one.

    Needs no session — this endpoint is public, which makes it a safe way to
    check the id before wiring anything else up.
    """
    config = Config.load()
    target = team_id or config.secrets.team_id
    if target is None:
        console.print(
            "[red]no team id given.[/red] Find it in the URL when viewing your points:\n"
            "[dim]https://fantasy.premierleague.com/entry/<THIS NUMBER>/event/1[/dim]"
        )
        raise typer.Exit(1)

    with FPLClient(config.cache_dir) as client:
        try:
            entry = client.entry(target, ttl=0)
        except FPLError as exc:
            console.print(f"[red]could not read entry {target}[/red]: {exc}")
            raise typer.Exit(1) from exc

    manager = f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}"
    console.print(
        f"[bold]{entry.get('name', '?')}[/bold] — {manager.strip()}\n"
        f"team id [bold]{target}[/bold] · "
        f"overall rank {entry.get('summary_overall_rank') or 'unranked'} · "
        f"{entry.get('summary_overall_points', 0)} points"
    )


@auth_app.command("refresh")
def auth_refresh(
    force: Annotated[
        bool, typer.Option("--force", help="Refresh even if the token is still valid")
    ] = False,
) -> None:
    """Mint a fresh access token from the stored refresh token.

    Worth running once by hand after capturing a session. The access token lasts
    an hour, so the refresh path is what every scheduled run depends on — and the
    worst time to discover it is broken is at a deadline.
    """
    config = Config.load()
    session = load_session(config)
    if session is None or not session.has_credentials:
        console.print("[yellow]no session found[/yellow] — run `arsenal auth attach`.")
        raise typer.Exit(1)

    if not session.tokens.can_refresh:
        console.print(
            "[red]no refresh token stored.[/red] Re-capture with `arsenal auth attach` "
            "while logged in — older captures saved only the access token."
        )
        raise typer.Exit(1)

    before = session.tokens.remaining()
    if before is not None and not force:
        minutes = before.total_seconds() / 60
        console.print(f"current token valid for {minutes:.0f} more minutes")

    if force:
        # Bypass the "not near expiry" check so the path can actually be tested.
        session.tokens = OidcTokens(
            access_token=None,
            refresh_token=session.tokens.refresh_token,
            client_id=session.tokens.client_id,
        )

    previous_refresh = session.tokens.refresh_token

    try:
        session, refreshed = refresh_if_needed(session)
    except TokenError as exc:
        console.print(f"[red]refresh failed[/red]: {exc}")
        raise typer.Exit(2) from exc

    if not refreshed:
        console.print("[dim]token is still fresh; nothing to do (use --force to test)[/dim]")
        raise typer.Exit(0)

    remaining = session.tokens.remaining()
    minutes = remaining.total_seconds() / 60 if remaining else 0
    console.print(
        f"[green]refreshed[/green] — new access token valid for {minutes:.0f} minutes"
    )
    console.print("[dim]saved to session.json and .env[/dim]")

    # Whether the provider rotates refresh tokens decides the whole CI design.
    # If it rotates, every automated run must write the new one back to wherever
    # the secret lives, or the following run authenticates with a dead token.
    if session.tokens.refresh_token != previous_refresh:
        console.print(
            "\n[yellow]the refresh token rotated[/yellow] — the provider issues a "
            "new one on each use.\n"
            "[dim]Automated runs must persist it after every refresh, or the next "
            "run starts with a revoked token.[/dim]"
        )
    else:
        console.print(
            "\n[green]the refresh token is stable[/green] — the same one survives "
            "a refresh.\n"
            "[dim]That makes CI simple: store it once as a secret and let each run "
            "mint its own access token.[/dim]"
        )

    _verify(session, config.secrets.team_id)

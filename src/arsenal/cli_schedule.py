"""``arsenal schedule`` — deciding whether anything is due right now.

The workflow wakes hourly and asks this. Scheduling is computed from the live
``deadline_time`` rather than a fixed cron, because FPL deadlines move for
midweek and holiday rounds: a cron pinned to Friday evening drifts away from the
real deadline within weeks and eventually misses one entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console

from .config import Config
from .session import authenticated_client

console = Console()


@dataclass(frozen=True)
class Stage:
    name: str
    description: str


STAGES = {
    "research": Stage("research", "full data pull and deep research"),
    "provisional": Stage("provisional", "forecast, optimise, notify a provisional plan"),
    "final": Stage("final", "late news sweep, re-forecast, re-optimise"),
    "submit": Stage("submit", "validate and submit"),
    "verify": Stage("verify", "read back and confirm the server agrees"),
    "idle": Stage("idle", "nothing due"),
}


def stage_for(hours_to_deadline: float, config: Config) -> Stage:
    """Which pipeline stage is due at this distance from the deadline.

    Ordered nearest-first: when several windows overlap, the most urgent wins.
    """
    schedule = config.schedule
    minutes = hours_to_deadline * 60

    if hours_to_deadline < 0:
        # After the deadline the gameweek is immutable. `is_next` will roll to
        # the following gameweek shortly, and normal scheduling resumes.
        return STAGES["idle"]
    if minutes <= schedule.abort_if_under_minutes:
        # Too close to recover from a failed write. Doing nothing is the correct
        # action: the team already picked stands.
        return STAGES["idle"]
    if minutes <= schedule.verify_minutes_before:
        return STAGES["verify"]
    if minutes <= schedule.submit_minutes_before:
        return STAGES["submit"]
    if hours_to_deadline <= schedule.final_research_hours_before:
        return STAGES["final"]
    if hours_to_deadline <= schedule.provisional_hours_before:
        return STAGES["provisional"]
    if hours_to_deadline <= schedule.research_hours_before:
        return STAGES["research"]
    return STAGES["idle"]


def schedule(
    output: Annotated[str, typer.Option("--output", help="human or github")] = "human",
) -> None:
    """Report which stage is due, for a scheduler to act on.

    ``--output github`` writes to ``$GITHUB_OUTPUT`` so a workflow step can gate
    on it, and exits 0 either way — "nothing due" is a normal result, not a
    failure, and a non-zero exit would paint the run red every hour.
    """
    config = Config.load()
    with authenticated_client(config, required=False) as client:
        bootstrap = client.bootstrap()

    nxt = bootstrap.next_event
    if nxt is None:
        _emit(output, STAGES["idle"], None, 0.0)
        return

    hours = (nxt.deadline_time - datetime.now(UTC)).total_seconds() / 3600
    _emit(output, stage_for(hours, config), nxt.id, hours)


def _emit(output: str, stage: Stage, gameweek: int | None, hours: float) -> None:
    if output == "github":
        target = os.environ.get("GITHUB_OUTPUT")
        line = f"stage={stage.name}\ngameweek={gameweek or ''}\nhours={hours:.1f}\n"
        if target:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line)
        # Also print, so the workflow log shows the decision rather than only
        # its consequence.
        console.print(f"stage={stage.name} gameweek={gameweek} T-{hours:.1f}h")
        return

    if gameweek is None:
        console.print("[yellow]no upcoming gameweek[/yellow]")
        return

    colour = "green" if stage.name != "idle" else "dim"
    console.print(
        f"GW{gameweek} · T−{hours:.1f}h · [{colour}]{stage.name}[/{colour}] — "
        f"{stage.description}"
    )

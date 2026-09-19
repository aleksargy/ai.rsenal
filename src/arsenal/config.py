"""Configuration: tunable policy from ``config.yaml``, secrets from the environment.

The split is deliberate. Policy — how big a hit to accept, how far ahead to plan
— belongs in version control where changes are reviewable and a bad season can be
traced to the setting that caused it. Secrets never touch the repo.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DATA_DIR = REPO_ROOT / "data"


@dataclass
class OptimiserSettings:
    horizon: int = 5
    """Gameweeks to plan over. Beyond ~5 the forecast is too noisy to be worth solving."""

    discount: float = 0.85
    """Per-gameweek discount. Near-term xP is more reliable and plans rarely survive."""

    risk_aversion: float = 0.2
    """κ in ``xP - κσ``. Stops the solver chasing volatile players' upper tails."""

    bench_weight: float = 0.1
    """Small bonus for bench quality, so the bench is not filled with £4.0m ghosts."""

    max_hit: int = 4
    """Most points to spend on transfers. 4 is one hit; 0 disables hits entirely."""

    solver_time_limit: int = 60
    """Seconds. A timeout is a failed stage that retries with a shorter horizon."""


@dataclass
class ResearchSettings:
    shortlist_size: int = 60
    """Candidates researched beyond the owned 15. The full 660 is wasteful."""

    youtube_channels: list[str] = field(default_factory=list)
    subreddits: list[str] = field(default_factory=lambda: ["FantasyPL"])
    max_evidence_age_days: int = 10
    """Availability claims older than this are discarded rather than downweighted."""

    min_sources: int = 2
    """Below this many working adapters, abort rather than forecast on thin evidence."""


@dataclass
class ScheduleSettings:
    research_hours_before: int = 72
    provisional_hours_before: int = 24
    final_research_hours_before: int = 3
    submit_minutes_before: int = 90
    verify_minutes_before: int = 30
    abort_if_under_minutes: int = 10
    """Refuse to submit inside this margin — too little room to recover from a failure."""


@dataclass
class AutonomySettings:
    enabled: bool = True
    """False puts the whole system in advisory mode regardless of session health."""

    auto_chips: bool = True
    """Whether chips fire without approval."""

    require_approval_for_wildcard: bool = True
    """Wildcard rewrites the entire squad; that is worth a human glance."""

    dry_run: bool = False
    """Print payloads instead of sending them. Always true until the first verified run."""


@dataclass
class Secrets:
    """Loaded from the environment. Never written to disk, never logged."""

    team_id: int | None = None
    session_cookies: dict[str, str] = field(default_factory=dict)
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    youtube_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        team_id = os.environ.get("FPL_TEAM_ID")
        session_raw = os.environ.get("FPL_SESSION_JSON", "")
        cookies: dict[str, str] = {}
        if session_raw:
            try:
                parsed = json.loads(session_raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "FPL_SESSION_JSON is not valid JSON. Regenerate it with "
                    "`arsenal auth export`."
                ) from exc
            # Accept either a flat cookie mapping or a Playwright storage_state.
            if isinstance(parsed, dict) and "cookies" in parsed:
                cookies = {
                    c["name"]: c["value"]
                    for c in parsed["cookies"]
                    if "premierleague.com" in c.get("domain", "")
                }
            elif isinstance(parsed, dict):
                cookies = {str(k): str(v) for k, v in parsed.items()}

        return cls(
            team_id=int(team_id) if team_id else None,
            session_cookies=cookies,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            youtube_api_key=os.environ.get("YOUTUBE_API_KEY"),
            reddit_client_id=os.environ.get("REDDIT_CLIENT_ID"),
            reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET"),
        )

    @property
    def has_session(self) -> bool:
        """Whether a session is present. Says nothing about whether it still works."""
        return bool(self.session_cookies)


@dataclass
class Config:
    optimiser: OptimiserSettings = field(default_factory=OptimiserSettings)
    research: ResearchSettings = field(default_factory=ResearchSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    autonomy: AutonomySettings = field(default_factory=AutonomySettings)
    secrets: Secrets = field(default_factory=Secrets)
    data_dir: Path = DATA_DIR

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    def run_dir(self, gameweek: int) -> Path:
        path = self.runs_dir / f"gw{gameweek:02d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load ``config.yaml`` if present, overlay environment secrets.

        A missing config file is fine — every setting has a working default.
        """
        config_path = path or DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        def section(name: str, cls_: type) -> Any:
            values = raw.get(name) or {}
            known = {k: v for k, v in values.items() if k in cls_.__dataclass_fields__}
            return cls_(**known)

        return cls(
            optimiser=section("optimiser", OptimiserSettings),
            research=section("research", ResearchSettings),
            schedule=section("schedule", ScheduleSettings),
            autonomy=section("autonomy", AutonomySettings),
            secrets=Secrets.from_env(),
            data_dir=Path(raw.get("data_dir", DATA_DIR)),
        )

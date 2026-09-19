"""Configuration: tunable policy from ``config.yaml``, secrets from the environment.

The split is deliberate. Policy — how big a hit to accept, how far ahead to plan
— belongs in version control where changes are reviewable and a bad season can be
traced to the setting that caused it. Secrets never touch the repo.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
DATA_DIR = REPO_ROOT / "data"


def load_dotenv(path: Path | None = None) -> int:
    """Load ``.env`` into the environment, without overwriting what is already set.

    Real environment variables always win. That ordering matters in CI: GitHub
    Actions injects secrets as environment variables, and a stale committed
    ``.env`` silently overriding them would be a miserable thing to debug.

    Deliberately not a dependency. The format here is ``KEY=value`` with optional
    ``export``, ``#`` comments, and optional surrounding quotes — which is all
    this project's secrets need, and a parser small enough to read in one sitting
    beats a library for that.
    """
    target = path or DEFAULT_ENV_PATH
    if not target.exists():
        return 0

    loaded = 0
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # Strip matching quotes, but leave inner ones alone — a session JSON
        # blob is full of them.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def update_env_file(values: dict[str, str], path: Path | None = None) -> Path:
    """Set keys in ``.env``, preserving everything else in the file.

    Exists because the alternative — printing a credential and asking the user to
    paste it — is bad on two counts. It puts a live token into terminal
    scrollback, and a long single-line JSON value gets wrapped by the terminal
    and pasted back as several lines, which no ``KEY=value`` parser can read.
    Writing the file directly avoids both.
    """
    target = path or DEFAULT_ENV_PATH
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        stripped = line.strip().removeprefix("export ").strip()
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)

    updated.extend(f"{key}={value}" for key, value in remaining.items())

    target.write_text("\n".join(updated) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        target.chmod(0o600)
    return target


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

    hit_margin: float = 3.0
    """Expected points a hit must clear beyond break-even before it is taken.

    The -4 is certain; the gain justifying it is a noisy forecast. Without a
    margin, every hit estimated at 4.1 gets taken and about half are really
    below 4."""

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

    max_tier_that_moves_forecast: int = 4
    """How far down the source tiers a claim may still change a number.

    4 (default) admits FPL creator and community opinion. It is weighted by what
    the claim asserts rather than flatly: a specialist's *judgement* about
    rotation or role counts for much more than their relaying of a *fact* the
    club already announced.

    Narrower than it sounds: a creator who *attributes* a claim to a press
    conference is already promoted to Tier 3 and counts either way. Only pure
    opinion is gated. Raise it and re-run `arsenal backtest` - that is the only
    way to find out whether it helps."""

    provider: str = "auto"
    """Which model provider extracts claims: auto, anthropic, or gemini.

    `auto` prefers Anthropic for quality and falls back to Gemini, so the
    pipeline uses whatever you have configured. Gemini has a genuinely free tier
    through Google AI Studio; Anthropic is paid but small."""

    model: str | None = None
    """Model id. None picks the provider default - claude-opus-5 or
    gemini-2.5-flash."""
    """Model used to extract claims from prose.

    Extraction quality sets P(plays), which dominates every forecast — a misread
    hedge costs more points than any amount of optimiser tuning, so the default
    is the most capable model. A full research run is roughly 51k input and 18k
    output tokens: about $0.71 on claude-opus-5, $0.28 on claude-sonnet-5, $0.14
    on claude-haiku-4-5. The trade is yours to make."""


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


def _oidc_from(parsed: Any) -> dict[str, Any]:
    """Pull the OIDC token block out of a parsed FPL_SESSION_JSON payload.

    Handles both the current shape (an `oidc` block carrying the refresh token)
    and the earlier one that stored a bare `access_token`.
    """
    if not isinstance(parsed, dict):
        return {}
    if isinstance(parsed.get("oidc"), dict):
        return dict(parsed["oidc"])
    if parsed.get("access_token"):
        return {"access_token": parsed["access_token"]}
    return {}


@dataclass
class Secrets:
    """Loaded from the environment. Never written to disk, never logged."""

    team_id: int | None = None
    session_cookies: dict[str, str] = field(default_factory=dict)
    oidc: dict[str, Any] = field(default_factory=dict)
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    youtube_api_key: str | None = None
    gemini_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        load_dotenv()
        team_id = os.environ.get("FPL_TEAM_ID")
        session_raw = os.environ.get("FPL_SESSION_JSON", "")
        cookies: dict[str, str] = {}
        # Bound before the branch: having no session at all is the normal state
        # on a fresh checkout, and it must not be the one path that crashes.
        parsed: Any = None
        if session_raw:
            try:
                parsed = json.loads(session_raw)
            except json.JSONDecodeError:
                # Degrade rather than raise. A mangled session should cost you
                # authenticated reads, not every command in the CLI — including
                # the `auth` commands you need to repair it.
                log.warning(
                    "FPL_SESSION_JSON is not valid JSON and has been ignored. "
                    "Re-capture with `arsenal auth attach`."
                )
                parsed = None
            # Accept either a flat cookie mapping or a Playwright storage_state.
            if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
                cookies = {
                    c["name"]: c["value"]
                    for c in parsed["cookies"]
                    if "premierleague.com" in c.get("domain", "")
                }
            elif isinstance(parsed, dict) and isinstance(parsed.get("cookies"), dict):
                cookies = {str(k): str(v) for k, v in parsed["cookies"].items()}
            elif isinstance(parsed, dict):
                cookies = {str(k): str(v) for k, v in parsed.items() if k != "access_token"}

        return cls(
            team_id=int(team_id) if team_id else None,
            session_cookies=cookies,
            oidc=_oidc_from(parsed),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            youtube_api_key=os.environ.get("YOUTUBE_API_KEY"),
            gemini_api_key=(
                os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            ),
            reddit_client_id=os.environ.get("REDDIT_CLIENT_ID"),
            reddit_client_secret=os.environ.get("REDDIT_CLIENT_SECRET"),
        )

    @property
    def access_token(self) -> str | None:
        return self.oidc.get("access_token")

    @property
    def has_session(self) -> bool:
        """Whether credentials are present. Says nothing about whether they work."""
        return bool(self.session_cookies) or bool(self.oidc.get("access_token"))


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

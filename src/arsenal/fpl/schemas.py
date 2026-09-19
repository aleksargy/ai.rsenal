"""Typed models for FPL API payloads — the defensive parse boundary.

The FPL API is undocumented, unversioned, and changes shape between seasons.
Everything crossing into the rest of the codebase is validated here, so a shape
change fails loudly in one place rather than producing a subtly wrong squad three
layers down.

Two deliberate choices:

* **Fields are optional unless the codebase genuinely cannot proceed without
  them.** A missing ``expected_goals`` should degrade a forecast, not kill a
  deadline run. A missing ``id`` or ``now_cost`` is unrecoverable and should
  raise.
* **Unknown fields are ignored, not rejected.** FPL adds fields mid-season
  (this season brought ``defensive_contribution`` and ``price_change_projections``).
  New fields must never break an existing run.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field


class Position(IntEnum):
    """``element_type`` ids. Stable across seasons, but read from the API anyway."""

    GKP = 1
    DEF = 2
    MID = 3
    FWD = 4

    @property
    def short(self) -> str:
        return self.name


class Availability:
    """``status`` codes on an element. Not an enum — FPL has added codes before."""

    AVAILABLE = "a"
    DOUBTFUL = "d"
    INJURED = "i"
    SUSPENDED = "s"
    NOT_ELIGIBLE = "n"  # on loan, or otherwise out of the game
    UNAVAILABLE = "u"  # left the league — must never appear in a squad


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Element(_Base):
    """A player. Only ``id``, ``element_type``, ``team`` and ``now_cost`` are required."""

    id: int
    element_type: Position
    team: int
    now_cost: int  # tenths of a million
    web_name: str = ""
    first_name: str = ""
    second_name: str = ""

    # Availability — club-sourced, Tier 1. Drives P(plays).
    status: str = Availability.AVAILABLE
    chance_of_playing_next_round: int | None = None
    chance_of_playing_this_round: int | None = None
    news: str = ""
    news_added: datetime | None = None
    can_transact: bool = True
    can_select: bool = True

    # Scoring history this season
    minutes: int = 0
    starts: int = 0
    total_points: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    goals_conceded: int = 0
    saves: int = 0
    bonus: int = 0
    bps: int = 0
    yellow_cards: int = 0
    red_cards: int = 0

    # Underlying (Opta, supplied to FPL) — predictors, never scorers
    expected_goals: float = 0.0
    expected_assists: float = 0.0
    expected_goal_involvements: float = 0.0
    expected_goals_conceded: float = 0.0
    expected_goals_per_90: float = 0.0
    expected_assists_per_90: float = 0.0
    expected_goal_involvements_per_90: float = 0.0
    expected_goals_conceded_per_90: float = 0.0

    # Defensive contribution — a threshold rule, see the `fpl-rules` skill
    defensive_contribution: int = 0
    defensive_contribution_per_90: float = 0.0
    clearances_blocks_interceptions: int = 0
    recoveries: int = 0
    tackles: int = 0

    # Set-piece duty: 1 = first choice, None = not a taker. Materially
    # undervalued by naive xG models.
    penalties_order: int | None = None
    direct_freekicks_order: int | None = None
    corners_and_indirect_freekicks_order: int | None = None

    # Market
    selected_by_percent: float = 0.0
    transfers_in_event: int = 0
    transfers_out_event: int = 0
    form: float = 0.0
    points_per_game: float = 0.0
    ep_this: float | None = None  # FPL's own expected points — a baseline to beat
    ep_next: float | None = None

    # Price movement
    cost_change_event: int = 0
    cost_change_start: int = 0

    @property
    def name(self) -> str:
        return self.web_name or f"{self.first_name} {self.second_name}".strip()

    @property
    def is_available(self) -> bool:
        """Safe to field. Doubtful players are available but should be risk-weighted."""
        return self.status in (Availability.AVAILABLE, Availability.DOUBTFUL)

    @property
    def is_transactable(self) -> bool:
        """Legal to own. ``status='u'`` players must never enter a squad."""
        return self.status != Availability.UNAVAILABLE and self.can_transact


class Team(_Base):
    """A Premier League club.

    The ``strength_*`` fields are FPL's own editorial team ratings and they are
    **largely unpopulated this season**: ``strength`` is null for every club, and
    ``strength_attack_*`` / ``strength_defence_*`` are all zero. Only
    ``strength_overall_home`` / ``_away`` carry real values, on a coarse 1-5
    scale.

    Do not build fixture difficulty on these. Compute it from rolling opponent
    xG-for and xG-against instead — see the ``fpl-fixture-analyst`` agent. They
    are typed permissively here so a mid-season repopulation does not break the
    parse.
    """

    id: int
    name: str = ""
    short_name: str = ""
    strength: int | None = None
    strength_overall_home: int | None = None
    strength_overall_away: int | None = None
    strength_attack_home: int | None = None
    strength_attack_away: int | None = None
    strength_defence_home: int | None = None
    strength_defence_away: int | None = None

    @property
    def has_usable_strength(self) -> bool:
        """Whether FPL's ratings are populated enough to be worth reading at all."""
        return any(
            v not in (None, 0)
            for v in (
                self.strength,
                self.strength_attack_home,
                self.strength_attack_away,
                self.strength_defence_home,
                self.strength_defence_away,
            )
        )


class Event(_Base):
    """A gameweek."""

    id: int
    name: str = ""
    deadline_time: datetime  # always UTC
    finished: bool = False
    data_checked: bool = False
    is_previous: bool = False
    is_current: bool = False
    is_next: bool = False
    average_entry_score: int = 0
    highest_score: int | None = None
    most_captained: int | None = None
    most_selected: int | None = None


class ElementTypeInfo(_Base):
    """Positional squad rules. Read these rather than hardcoding 2/5/5/3."""

    id: int
    singular_name_short: str = ""
    squad_select: int  # how many of this position in the 15
    squad_min_play: int  # minimum in the starting XI
    squad_max_play: int  # maximum in the starting XI


class Chip(_Base):
    id: int
    name: str  # wildcard | freehit | bboost | 3xc
    number: int
    start_event: int
    stop_event: int
    chip_type: str  # "transfer" or "team"


class ScoringConfig(_Base):
    """``game_config.scoring`` — the live scoring table the FPL engine runs on.

    Authoritative. Prefer this over any documentation, blog, or recollection;
    scoring rules change most seasons.
    """

    long_play: int = 2
    short_play: int = 1
    goals_scored: dict[str, int] = Field(default_factory=dict)
    assists: int = 3
    clean_sheets: dict[str, int] = Field(default_factory=dict)
    goals_conceded: dict[str, int] = Field(default_factory=dict)
    defensive_contribution: dict[str, int] = Field(default_factory=dict)
    saves: int = 1
    penalties_saved: int = 5
    penalties_missed: int = -2
    yellow_cards: int = -1
    red_cards: int = -3
    own_goals: int = -2
    bonus: int = 1


class RulesConfig(_Base):
    """``game_config.rules`` — squad and transfer constraints, from the engine."""

    squad_squadsize: int = 15
    squad_squadplay: int = 11
    squad_team_limit: int = 3
    squad_total_spend: int = 1000  # tenths
    transfers_cap: int = 20
    transfers_sell_on_fee: float = 0.5
    max_extra_free_transfers: int = 4
    element_sell_at_purchase_price: bool = False

    @property
    def max_free_transfers(self) -> int:
        """Maximum bankable free transfers: the extras, plus the current week's one."""
        return self.max_extra_free_transfers + 1


class GameConfig(_Base):
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)


class Bootstrap(_Base):
    """``/api/bootstrap-static/`` — the main payload."""

    elements: list[Element]
    teams: list[Team]
    events: list[Event]
    element_types: list[ElementTypeInfo]
    chips: list[Chip] = Field(default_factory=list)
    game_config: GameConfig = Field(default_factory=GameConfig)

    def element_by_id(self) -> dict[int, Element]:
        return {e.id: e for e in self.elements}

    def team_by_id(self) -> dict[int, Team]:
        return {t.id: t for t in self.teams}

    @property
    def current_event(self) -> Event | None:
        return next((e for e in self.events if e.is_current), None)

    @property
    def next_event(self) -> Event | None:
        """The gameweek we are planning for — the next one with an open deadline."""
        return next((e for e in self.events if e.is_next), None)

    def squad_requirements(self) -> dict[Position, int]:
        """How many of each position make up the 15, read from the API."""
        return {Position(t.id): t.squad_select for t in self.element_types}

    def play_limits(self) -> dict[Position, tuple[int, int]]:
        """(min, max) of each position in the starting XI, read from the API."""
        return {
            Position(t.id): (t.squad_min_play, t.squad_max_play) for t in self.element_types
        }


class Fixture(_Base):
    id: int
    event: int | None = None  # None when unscheduled — common late in the season
    team_h: int
    team_a: int
    team_h_score: int | None = None
    team_a_score: int | None = None
    team_h_difficulty: int = 3  # FDR: coarse editorial rating, a weak prior at best
    team_a_difficulty: int = 3
    kickoff_time: datetime | None = None
    finished: bool = False
    started: bool = False


class Pick(_Base):
    """One pick from the authenticated ``/api/my-team/{id}/`` endpoint."""

    element: int
    position: int  # 1-11 start, 12-15 bench in order, 15 = reserve GK
    purchase_price: int  # tenths — only available here, needed for sell-on fee
    selling_price: int  # tenths — what FPL says it sells for; trust over recomputation
    is_captain: bool = False
    is_vice_captain: bool = False
    multiplier: int = 1


class TransferInfo(_Base):
    bank: int = 0  # tenths
    value: int = 0  # tenths, squad value at *purchase*, not selling, prices
    limit: int | None = None  # free transfers available; None under an active chip
    made: int = 0


class MyTeam(_Base):
    """``/api/my-team/{id}/`` — requires authentication.

    The only source of ``purchase_price``, and therefore the only way to compute a
    legal budget. If this cannot be read, a run must abort rather than guess.
    """

    picks: list[Pick]
    transfers: TransferInfo = Field(default_factory=TransferInfo)
    chips: list[dict[str, object]] = Field(default_factory=list)

"""Resolving player names to FPL element ids.

This is the classic failure point of an FPL research pipeline, and it fails
quietly. Auto-generated YouTube captions mangle names constantly; FBref and
Understat spell them differently from FPL; several players share a surname; and
transfers move players between clubs mid-season. A resolver that guesses will
happily attribute a Manchester City injury report to a Brentford defender, and
nothing downstream will ever notice.

So the rule here is: **resolve confidently or not at all.** An ambiguous name is
dropped and logged, never guessed. Dropping a claim costs one piece of evidence;
misattributing it corrupts a forecast and can cost a transfer.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from ..fpl.schemas import Bootstrap, Element

log = logging.getLogger(__name__)


def normalise(name: str) -> str:
    """Fold a name to a comparable form.

    Strips accents (Ødegaard/Odegaard, Gündoğan/Gundogan), punctuation and case.
    Without this, the same player arrives under three spellings from three
    sources and resolves under none of them.
    """
    # NFKD splits accented characters into base + combining mark, which the
    # ASCII encode then discards. Ø has no decomposition, so it is mapped first.
    folded = name.replace("Ø", "O").replace("ø", "o").replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFKD", folded)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z ]", " ", ascii_only.lower()).strip()


def _tokens(name: str) -> list[str]:
    return [part for part in normalise(name).split() if len(part) > 1]


@dataclass
class Resolution:
    """The outcome of a single lookup."""

    element_id: int | None
    reason: str
    candidates: list[int] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.element_id is not None


@dataclass
class PlayerResolver:
    """Name → ``element_id``, built from the live player list.

    Lookups are tried strongest-first: exact full name, exact web name, then
    surname. A surname match is only accepted when it is **unique**, or when a
    club hint disambiguates it.
    """

    elements: list[Element]
    team_codes: dict[int, str] = field(default_factory=dict)

    _by_full: dict[str, list[int]] = field(default_factory=dict, init=False)
    _by_web: dict[str, list[int]] = field(default_factory=dict, init=False)
    _by_surname: dict[str, list[int]] = field(default_factory=dict, init=False)
    _by_id: dict[int, Element] = field(default_factory=dict, init=False)
    _clubs: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for element in self.elements:
            self._by_id[element.id] = element
            full = normalise(f"{element.first_name} {element.second_name}")
            web = normalise(element.web_name)
            if full:
                self._by_full.setdefault(full, []).append(element.id)
            if web:
                self._by_web.setdefault(web, []).append(element.id)
            surname = _tokens(element.second_name or element.web_name)
            if surname:
                self._by_surname.setdefault(surname[-1], []).append(element.id)

        for team_id, short_name in self.team_codes.items():
            self._clubs[normalise(short_name)] = team_id

    @classmethod
    def from_bootstrap(cls, bootstrap: Bootstrap) -> PlayerResolver:
        return cls(
            elements=list(bootstrap.elements),
            team_codes={team.id: team.short_name for team in bootstrap.teams},
        )

    def _filter_by_team(self, ids: list[int], team_id: int | None) -> list[int]:
        if team_id is None:
            return ids
        narrowed = [i for i in ids if self._by_id[i].team == team_id]
        return narrowed or ids

    def resolve(self, name: str, *, team_id: int | None = None) -> Resolution:
        """Resolve one name, optionally disambiguated by club.

        Returns an unresolved :class:`Resolution` rather than raising — a single
        unresolvable name should cost one claim, not a whole research run.
        """
        cleaned = normalise(name)
        if not cleaned:
            return Resolution(None, "empty name")

        parts = _tokens(cleaned)
        if not parts:
            return Resolution(None, "no usable tokens")

        # A multi-token name ("Bernardo Silva", "B.Silva") is specific enough that
        # an exact index hit settles it. Specificity is judged on the *raw* token
        # count, not the filtered one: `_tokens` drops single characters, so
        # "B.Silva" would otherwise look like the bare surname "Silva" and be
        # rejected as ambiguous despite naming exactly one player.
        is_specific = len(cleaned.split()) > 1

        if is_specific:
            for index, label in ((self._by_full, "full name"), (self._by_web, "web name")):
                matches = index.get(cleaned)
                if matches:
                    narrowed = self._filter_by_team(matches, team_id)
                    if len(narrowed) == 1:
                        return Resolution(narrowed[0], label)
                    return Resolution(
                        None,
                        f"{label} '{name}' matches {len(narrowed)} players; "
                        "a club hint is required",
                        narrowed,
                    )

        # A bare single token is how prose usually refers to players, and it is
        # where misattribution happens. An exact web-name hit is NOT conclusive
        # here: FPL assigns the bare surname as web_name to whichever player
        # claimed it first, so "Silva" matches João Silva exactly while Bernardo
        # Silva sits behind "B.Silva". Resolving on the web name alone would
        # confidently attribute a Bernardo Silva report to João.
        #
        # So the candidate set is the union of everyone the token could denote —
        # by web name or by surname — and the match must be unique across it.
        candidates = sorted(
            set(self._by_web.get(cleaned, [])) | set(self._by_surname.get(parts[-1], []))
        )
        if not candidates:
            return Resolution(None, f"no player matches '{name}'")

        narrowed = self._filter_by_team(candidates, team_id)
        if len(narrowed) == 1:
            return Resolution(narrowed[0], "surname")

        # Several players share the surname. Try the forename before giving up.
        if len(parts) > 1:
            forename = parts[0]
            by_forename = [
                i for i in narrowed if normalise(self._by_id[i].first_name).startswith(forename)
            ]
            if len(by_forename) == 1:
                return Resolution(by_forename[0], "surname and forename")

        return Resolution(
            None,
            f"'{name}' is ambiguous between {len(narrowed)} players; "
            "dropped rather than guessed",
            narrowed,
        )

    def resolve_club(self, name: str) -> int | None:
        """Resolve a club name or short code to a team id."""
        cleaned = normalise(name)
        if cleaned in self._clubs:
            return self._clubs[cleaned]
        for club_name, team_id in self._clubs.items():
            if cleaned.startswith(club_name) or club_name.startswith(cleaned):
                return team_id
        return None

    def describe(self, element_id: int) -> str:
        element = self._by_id.get(element_id)
        if element is None:
            return f"unknown player {element_id}"
        club = self.team_codes.get(element.team, "?")
        return f"{element.name} ({club})"


def resolve_all(
    resolver: PlayerResolver, names: list[str], *, team_id: int | None = None
) -> tuple[dict[str, int], list[str]]:
    """Resolve a batch, returning the successes and the names that were dropped.

    The dropped list is returned rather than swallowed so a run can report how
    much evidence it lost. A resolver silently discarding half its input looks
    identical to one working perfectly.
    """
    resolved: dict[str, int] = {}
    dropped: list[str] = []
    for name in names:
        outcome = resolver.resolve(name, team_id=team_id)
        if outcome.resolved and outcome.element_id is not None:
            resolved[name] = outcome.element_id
        else:
            dropped.append(name)
            log.info("unresolved player name %r: %s", name, outcome.reason)
    return resolved, dropped

"""Research tests.

Two things carry real risk here and get most of the attention: the tier rules
(a confident YouTuber must never move a number) and the resolver (a misattributed
claim corrupts a forecast silently). Both are enforced in code, so both are
tested as behaviour rather than as documentation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from arsenal.fpl.schemas import Element, Position
from arsenal.research import (
    Evidence,
    PlayerResolver,
    Tier,
    build_report,
    deduplicate,
    may_adjust_forecast,
    normalise,
)
from arsenal.research.apply import MIN_AVAILABILITY_MULTIPLIER, apply_to_forecasts

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def make_evidence(**changes) -> Evidence:
    base = {
        "player_id": 1,
        "claim": "Injured and will miss the next match",
        "tier": Tier.FACT,
        "impact": "availability",
        "source_url": "https://example.com/news",
        "source_name": "Test Source",
        "published_at": NOW - timedelta(days=1),
        "confidence": 1.0,
    }
    return Evidence(**(base | changes))


def make_element(element_id: int, first: str, second: str, web: str, team: int = 1) -> Element:
    return Element.model_validate(
        {
            "id": element_id,
            "element_type": Position.MID,
            "team": team,
            "now_cost": 70,
            "first_name": first,
            "second_name": second,
            "web_name": web,
        }
    )


class TestEvidenceValidation:
    def test_requires_a_citable_source(self) -> None:
        """A claim you cannot cite is a claim you cannot use."""
        with pytest.raises(ValidationError, match="source_url"):
            make_evidence(source_url="I remember reading this somewhere")

    def test_rejects_naive_timestamps(self) -> None:
        """A naive timestamp compares wrong against a UTC deadline."""
        with pytest.raises(ValidationError, match="timezone-aware"):
            make_evidence(published_at=datetime(2026, 9, 18, 12, 0))

    def test_accepts_the_internal_fpl_scheme(self) -> None:
        assert make_evidence(source_url="fpl://element/42").source_url.startswith("fpl://")


class TestTierRules:
    def test_tier_four_is_gated_by_default(self) -> None:
        """Unattributed opinion does not move a number unless you ask it to.

        Note the scope: a creator who *attributes* a claim to a press conference
        is promoted to Tier 3 by the extractor and counts either way. Only pure
        opinion is gated here.
        """
        opinion = make_evidence(tier=Tier.OPINION, confidence=1.0)
        assert opinion.may_move_forecast() is False
        assert may_adjust_forecast(opinion, now=NOW) is False

    def test_tier_four_can_be_admitted_deliberately(self) -> None:
        """The threshold is policy, not doctrine.

        We hold no track record for any individual source, so the default is a
        starting position to be tested rather than a verdict. Raising it lets the
        backtest settle whether community opinion actually helps.
        """
        opinion = make_evidence(tier=Tier.OPINION, confidence=1.0)
        assert opinion.may_move_forecast(Tier.OPINION) is True
        assert may_adjust_forecast(opinion, now=NOW, max_tier=Tier.OPINION) is True

    def test_admitted_opinion_carries_less_weight_than_a_reporter(self) -> None:
        """Letting it count is not the same as trusting it equally."""
        claim = make_evidence(tier=Tier.OPINION, confidence=1.0)
        reported = make_evidence(tier=Tier.REPORTED, confidence=1.0)
        opinion_report = build_report([claim], now=NOW, max_tier=Tier.OPINION)
        reporter_report = build_report([reported], now=NOW)
        assert (
            opinion_report.adjustments[1].availability_multiplier
            > reporter_report.adjustments[1].availability_multiplier
        )

    @pytest.mark.parametrize("tier", [Tier.FACT, Tier.MEASURED, Tier.REPORTED])
    def test_tiers_one_to_three_may_move_a_forecast(self, tier: Tier) -> None:
        assert may_adjust_forecast(make_evidence(tier=tier), now=NOW) is True

    def test_stale_availability_is_discarded(self) -> None:
        """A Tuesday injury report is superseded by Friday's press conference."""
        old = make_evidence(published_at=NOW - timedelta(days=30))
        assert old.is_stale(now=NOW) is True
        assert may_adjust_forecast(old, now=NOW) is False

    def test_slow_decaying_impacts_get_a_longer_window(self) -> None:
        """Set-piece duty ages far more gracefully than an injury report."""
        fifteen_days = NOW - timedelta(days=15)
        assert make_evidence(published_at=fifteen_days, impact="availability").is_stale(now=NOW)
        assert not make_evidence(published_at=fifteen_days, impact="set_pieces").is_stale(
            now=NOW
        )

    def test_claim_without_a_subject_cannot_be_applied(self) -> None:
        orphan = make_evidence(player_id=None, team_id=None)
        assert may_adjust_forecast(orphan, now=NOW) is False


class TestDeduplication:
    def test_five_outlets_reporting_one_presser_count_once(self) -> None:
        """Repetition is not corroboration.

        Counting the same claim five times manufactures a consensus that does not
        exist — which is exactly how a rumour becomes a 'fact'.
        """
        same = [
            make_evidence(source_url=f"https://outlet{n}.com", source_name=f"Outlet {n}")
            for n in range(5)
        ]
        assert len(deduplicate(same)) == 1

    def test_strongest_tier_wins(self) -> None:
        records = [
            make_evidence(tier=Tier.OPINION, source_url="https://a.com"),
            make_evidence(tier=Tier.FACT, source_url="https://b.com"),
        ]
        assert deduplicate(records)[0].tier == Tier.FACT

    def test_different_claims_are_kept_apart(self) -> None:
        records = [
            make_evidence(claim="Injured and will miss the next match"),
            make_evidence(claim="Takes penalties", impact="set_pieces"),
        ]
        assert len(deduplicate(records)) == 2


class TestAdjustments:
    def test_official_injury_news_rules_a_player_out(self) -> None:
        report = build_report([make_evidence()], now=NOW)
        adjustment = report.adjustments[1]
        assert adjustment.availability_multiplier < 0.5
        assert adjustment.reasons

    def test_opinion_changes_nothing_but_is_kept_as_a_hypothesis(self) -> None:
        """Tier 4's job is to surface things to verify, not to move numbers."""
        report = build_report([make_evidence(tier=Tier.OPINION)], now=NOW)
        assert report.adjustments[1].availability_multiplier == 1.0
        assert report.adjustments[1].changed is False
        assert len(report.hypotheses) == 1

    def test_hedged_claims_carry_less_force(self) -> None:
        """'Should miss out' is not 'will miss out'."""
        firm = build_report([make_evidence(tier=Tier.REPORTED, hedged=False)], now=NOW)
        hedged = build_report([make_evidence(tier=Tier.REPORTED, hedged=True)], now=NOW)
        assert (
            hedged.adjustments[1].availability_multiplier
            > firm.adjustments[1].availability_multiplier
        )

    def test_a_reporter_cannot_rule_a_player_out_as_hard_as_the_club(self) -> None:
        fact = build_report([make_evidence(tier=Tier.FACT)], now=NOW)
        reported = build_report([make_evidence(tier=Tier.REPORTED)], now=NOW)
        assert (
            reported.adjustments[1].availability_multiplier
            > fact.adjustments[1].availability_multiplier
        )

    def test_a_single_claim_cannot_zero_a_player_out(self) -> None:
        """The extractor is a model reading noisy prose and will sometimes be wrong."""
        report = build_report([make_evidence(tier=Tier.REPORTED, confidence=1.0)], now=NOW)
        assert report.adjustments[1].availability_multiplier >= MIN_AVAILABILITY_MULTIPLIER

    def test_good_news_cannot_push_past_certainty(self) -> None:
        report = build_report(
            [make_evidence(claim="Fit and available, trained fully this week")], now=NOW
        )
        assert report.adjustments[1].availability_multiplier <= 1.0

    def test_stale_evidence_is_counted_and_discarded(self) -> None:
        report = build_report([make_evidence(published_at=NOW - timedelta(days=40))], now=NOW)
        assert report.stale_dropped == 1
        assert report.adjustments[1].changed is False

    def test_form_evidence_does_not_double_count(self) -> None:
        """Form is already in the statistical model; applying it again inflates it."""
        report = build_report(
            [make_evidence(impact="form", claim="In excellent form")], now=NOW
        )
        assert report.adjustments[1].availability_multiplier == 1.0


class TestConflicts:
    def test_disagreement_widens_uncertainty_rather_than_picking_a_winner(self) -> None:
        records = [
            make_evidence(
                tier=Tier.REPORTED,
                claim="Ruled out of the weekend fixture",
                hedged=False,
                source_url="https://a.com",
                source_name="Reporter A",
            ),
            make_evidence(
                tier=Tier.REPORTED,
                claim="Ruled out of the weekend fixture",
                hedged=True,
                source_url="https://b.com",
                source_name="Reporter B",
            ),
        ]
        report = build_report(records, now=NOW)
        assert report.conflicts
        assert report.adjustments[1].sigma_multiplier > 1.0


class TestApplyToForecasts:
    def forecast(self):
        from arsenal.forecast.minutes import MinutesForecast
        from arsenal.forecast.model import PlayerForecast, PointsBreakdown

        return PlayerForecast(
            element_id=1,
            position=Position.MID,
            team=1,
            name="Test",
            minutes=[MinutesForecast(p_appear=0.9, p_sixty=0.85, expected_minutes=80.0)],
            breakdowns=[PointsBreakdown(appearance=1.75, goals=2.0, bonus=0.5)],
        )

    def test_availability_scales_every_component(self) -> None:
        """A player who does not play scores none of it, not some of it."""
        forecasts = {1: self.forecast()}
        before = forecasts[1].xp[0]
        report = build_report([make_evidence()], now=NOW)
        apply_to_forecasts(forecasts, report)
        after = forecasts[1].xp[0]
        assert after < before
        assert forecasts[1].minutes[0].p_appear < 0.9

    def test_opinion_leaves_the_forecast_untouched(self) -> None:
        forecasts = {1: self.forecast()}
        before = forecasts[1].xp[0]
        apply_to_forecasts(forecasts, build_report([make_evidence(tier=Tier.OPINION)], now=NOW))
        assert forecasts[1].xp[0] == before


class TestResolver:
    def resolver(self) -> PlayerResolver:
        return PlayerResolver(
            elements=[
                make_element(1, "Bukayo", "Saka", "Saka", team=1),
                make_element(2, "Martin", "Ødegaard", "Ødegaard", team=1),
                make_element(3, "Douglas", "Luiz", "D.Luiz", team=2),
                # Two players who share a surname, in different clubs.
                make_element(4, "Diogo", "Dalot", "Dalot", team=3),
                make_element(5, "Joao", "Silva", "Silva", team=3),
                make_element(6, "Bernardo", "Silva", "B.Silva", team=4),
            ],
            team_codes={1: "ARS", 2: "AVL", 3: "MUN", 4: "MCI"},
        )

    def test_resolves_an_exact_name(self) -> None:
        assert self.resolver().resolve("Bukayo Saka").element_id == 1

    def test_resolves_by_surname(self) -> None:
        assert self.resolver().resolve("Saka").element_id == 1

    @pytest.mark.parametrize("spelling", ["Ødegaard", "Odegaard", "odegaard", "ØDEGAARD"])
    def test_handles_accents_and_case(self, spelling: str) -> None:
        """Sources spell the same player three different ways."""
        assert self.resolver().resolve(spelling).element_id == 2

    def test_ambiguous_surname_is_dropped_not_guessed(self) -> None:
        """Two players named Silva. Guessing corrupts a forecast silently."""
        outcome = self.resolver().resolve("Silva")
        assert outcome.resolved is False
        assert "ambiguous" in outcome.reason
        assert len(outcome.candidates) == 2

    def test_an_exact_web_name_does_not_override_a_shared_surname(self) -> None:
        """Regression: FPL gives the bare surname to whoever claimed it first.

        João Silva's ``web_name`` is literally "Silva" while Bernardo Silva sits
        behind "B.Silva". An exact web-name lookup therefore matched "Silva" to
        João with full confidence — silently attributing any Bernardo Silva
        report to the wrong player. A single bare token must be checked against
        everyone who could answer to it, not just the exact web-name index.
        """
        outcome = self.resolver().resolve("Silva")
        assert outcome.resolved is False
        assert set(outcome.candidates) == {5, 6}

    def test_a_distinct_web_name_still_resolves(self) -> None:
        """The fix must not break names that genuinely are unambiguous."""
        assert self.resolver().resolve("B.Silva").element_id == 6
        assert self.resolver().resolve("D.Luiz").element_id == 3

    def test_club_hint_disambiguates(self) -> None:
        assert self.resolver().resolve("Silva", team_id=4).element_id == 6

    def test_forename_disambiguates(self) -> None:
        assert self.resolver().resolve("Bernardo Silva").element_id == 6

    def test_unknown_name_returns_a_reason(self) -> None:
        outcome = self.resolver().resolve("Lionel Messi")
        assert outcome.resolved is False
        assert "no player matches" in outcome.reason

    def test_resolves_club_codes(self) -> None:
        assert self.resolver().resolve_club("ARS") == 1
        assert self.resolver().resolve_club("mci") == 4

    def test_empty_name_is_handled(self) -> None:
        assert self.resolver().resolve("").resolved is False
        assert self.resolver().resolve("   ").resolved is False


class TestNormalise:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Ødegaard", "odegaard"),
            ("Gündoğan", "gundogan"),
            ("N'Golo Kanté", "n golo kante"),
            ("  Saka  ", "saka"),
            ("Vinícius Júnior", "vinicius junior"),
        ],
    )
    def test_folds_to_a_comparable_form(self, raw: str, expected: str) -> None:
        assert normalise(raw) == expected


class TestTierOneIsNotModelMediated:
    """Regressions for two errors found running against live FPL data."""

    def test_official_status_can_rule_a_player_out_completely(self) -> None:
        """The safety floor defends against a model misreading prose.

        Tier 1 is read mechanically from the API, so it is not what the floor is
        for. Holding a suspended player at 25% availability would let the
        optimiser field him.
        """
        report = build_report([make_evidence(tier=Tier.FACT, confidence=1.0)], now=NOW)
        assert report.adjustments[1].availability_multiplier == 0.0

    def test_model_mediated_evidence_still_respects_the_floor(self) -> None:
        report = build_report([make_evidence(tier=Tier.REPORTED, confidence=1.0)], now=NOW)
        assert report.adjustments[1].availability_multiplier >= MIN_AVAILABILITY_MULTIPLIER


class TestLiveStateIsNotADatedReport:
    def test_fpl_status_evidence_is_timestamped_as_a_current_observation(self) -> None:
        """A long-term injury must not age out of the evidence set.

        `status` describes the player right now. Timestamping it with
        `news_added` made an injury announced in August look stale, and the
        staleness rule discarded it — so a player out indefinitely silently
        stopped counting as injured.
        """
        from arsenal.fpl.schemas import Bootstrap
        from arsenal.research import FPLNewsSource, PlayerResolver

        long_term_injury = Element.model_validate(
            {
                "id": 1,
                "element_type": Position.MID,
                "team": 1,
                "now_cost": 70,
                "web_name": "Crocked",
                "status": "i",
                "news": "Thigh injury - Unknown return date",
                "news_added": "2026-08-01T10:00:00Z",
            }
        )
        bootstrap = Bootstrap.model_validate(
            {
                "elements": [long_term_injury.model_dump()],
                "teams": [{"id": 1, "short_name": "ARS"}],
                "events": [],
                "element_types": [
                    {"id": 3, "squad_select": 5, "squad_min_play": 2, "squad_max_play": 5}
                ],
            }
        )
        result = FPLNewsSource(bootstrap).gather(PlayerResolver.from_bootstrap(bootstrap))
        assert len(result.evidence) == 1
        record = result.evidence[0]
        assert not record.is_stale(), "a current injury must not be discarded as stale"
        # The announcement date is still preserved, just not used as the timestamp.
        assert "announced 01 Aug" in record.claim


class TestGameweekScoping:
    """Week-specific claims must not apply to every week.

    `scout_risks` carries loan ineligibility scoped to one gameweek — a player
    barred from facing their parent club in GW27 is perfectly available in GW6.
    Applying such a claim unscoped benched a fit player for the rest of the
    season, which is exactly what happened before this was wired up.
    """

    def scoped(self, gameweek: int) -> Evidence:
        return make_evidence(
            claim="Unavailable: cannot face their parent club as a loan condition",
            gameweek=gameweek,
        )

    def test_applies_only_to_its_own_gameweek(self) -> None:
        claim = self.scoped(27)
        assert claim.applies_to(27) is True
        assert claim.applies_to(6) is False

    def test_unscoped_claims_apply_everywhere(self) -> None:
        assert make_evidence().applies_to(6) is True
        assert make_evidence().applies_to(27) is True

    def test_out_of_scope_claims_do_not_adjust(self) -> None:
        report = build_report([self.scoped(27)], now=NOW, gameweek=6)
        assert report.out_of_scope == 1
        # No adjustment entry at all, rather than an entry that happens to be
        # neutral — a player with no applicable evidence has not been assessed.
        assert 1 not in report.adjustments
        assert report.changed_players == []

    def test_in_scope_claims_do_adjust(self) -> None:
        report = build_report([self.scoped(27)], now=NOW, gameweek=27)
        assert report.out_of_scope == 0
        assert report.adjustments[1].availability_multiplier == 0.0

    def test_a_future_dated_claim_is_never_stale(self) -> None:
        """A scheduled fact does not perish the way a fitness report does."""
        old_but_scoped = make_evidence(
            published_at=NOW - timedelta(days=90),
            gameweek=27,
            claim="Unavailable: loan conditions",
        )
        assert old_but_scoped.is_stale(now=NOW) is False

    def test_no_gameweek_given_means_no_filtering(self) -> None:
        """Callers that do not know the gameweek must not silently drop evidence."""
        report = build_report([self.scoped(27)], now=NOW, gameweek=None)
        assert report.out_of_scope == 0


class TestHtmlToText:
    """Turning club-site HTML into something an extractor can read."""

    def test_decodes_all_entities(self) -> None:
        """Hand-rolled replacements missed numeric entities.

        Sangaré arrived at the extractor as "Sangar&#233;", which then fails to
        resolve to an element id and the claim is dropped.
        """
        from arsenal.research import html_to_text

        assert "Sangaré" in html_to_text("<p>Mamadou Sangar&#233; starts</p>")
        assert "&" in html_to_text("<p>Tottenham &amp; Brentford</p>")

    def test_strips_scripts_and_styles(self) -> None:
        from arsenal.research import html_to_text

        markup = "<script>var x = 'injured';</script><style>p{}</style><p>Real text</p>"
        result = html_to_text(markup)
        assert "Real text" in result
        assert "var x" not in result

    def test_collapses_blank_lines(self) -> None:
        from arsenal.research import html_to_text

        assert "\n\n\n" not in html_to_text("<div><p>a</p><br><br><br><p>b</p></div>")


class TestClubArticleGrouping:
    def test_groups_shared_links_and_strips_tracking(self) -> None:
        """One press conference covers a whole club's flagged players.

        FPL appends per-player UTM parameters, so grouping on the raw URL would
        fetch the same article six times.
        """
        from arsenal.research import club_article_links

        base = "https://www.avfc.co.uk/news/prematch-team-news/"
        raw = [
            {"id": 1, "team": 2, "scout_news_link": f"{base}?utm_content=a"},
            {"id": 2, "team": 2, "scout_news_link": f"{base}?utm_content=b"},
            {"id": 3, "team": 2, "scout_news_link": None},
        ]
        articles = club_article_links(raw, {2: "AVL"})
        assert len(articles) == 1
        assert articles[0].url == base
        assert sorted(articles[0].player_ids) == [1, 2]
        assert articles[0].club == "AVL"

    def test_players_without_a_link_are_skipped(self) -> None:
        from arsenal.research import club_article_links

        assert club_article_links([{"id": 1, "team": 2}], {2: "AVL"}) == []


class TestNoStalePropertyAccess:
    """Guard against a property becoming a method and callers not noticing.

    `Evidence.may_move_forecast` changed from a property to a method when the
    tier threshold became configurable. Every remaining `if e.may_move_forecast`
    then evaluated a bound method — always truthy, silently wrong, and no test
    failed because the value was only used for a displayed count.

    A bare reference to a known-callable attribute is almost always this mistake.
    """

    CALLABLE_ATTRS = ("may_move_forecast", "applies_to", "is_stale", "age")

    def test_callables_are_never_referenced_bare(self) -> None:
        import ast
        import inspect

        from arsenal.research import apply, evidence, extract, sources

        offenders: list[str] = []
        for module in (evidence, apply, extract, sources):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if node.attr not in self.CALLABLE_ATTRS:
                    continue
                # A bare attribute access outside a Call and outside a `def` is
                # the bug; `self.x()` parses as Call(func=Attribute(...)).
                parents = [
                    n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.func is node
                ]
                if not parents:
                    offenders.append(f"{module.__name__}:{node.lineno} .{node.attr}")

        assert not offenders, "these look like methods used as properties: " + ", ".join(
            offenders
        )


class TestCompoundSurnames:
    """FPL stores surnames in full; the world uses a fragment.

    Ezri Konsa is "Konsa Ngoyo" and Bruno Guimarães is "Guimarães Rodriguez
    Moura". Indexing only the last token filed them under "ngoyo" and "moura",
    so neither resolved from prose — and the same was true of a large share of
    the league's Portuguese, Spanish and African names. Real press-conference
    claims about both were being dropped.
    """

    def resolver(self) -> PlayerResolver:
        return PlayerResolver(
            elements=[
                make_element(1, "Ezri", "Konsa Ngoyo", "Konsa", team=1),
                make_element(2, "Bruno", "Guimarães Rodriguez Moura", "Bruno G.", team=2),
                make_element(3, "Bukayo", "Saka", "Saka", team=1),
                # Two players who collide on a shared first surname token.
                make_element(4, "Joao", "Silva Santos", "J.Silva", team=3),
                make_element(5, "Bernardo", "Silva Costa", "B.Silva", team=4),
            ],
            team_codes={1: "ARS", 2: "NEW", 3: "MUN", 4: "MCI"},
        )

    def test_resolves_the_first_token_of_a_compound_surname(self) -> None:
        assert self.resolver().resolve("Konsa").element_id == 1
        assert self.resolver().resolve("Guimaraes").element_id == 2

    def test_resolves_a_partial_full_name(self) -> None:
        """ "Ezri Konsa" never equals "Ezri Konsa Ngoyo", but its tokens are a subset."""
        assert self.resolver().resolve("Ezri Konsa").element_id == 1
        assert self.resolver().resolve("Bruno Guimaraes").element_id == 2

    def test_accents_still_fold(self) -> None:
        assert self.resolver().resolve("Bruno Guimarães").element_id == 2

    def test_a_simple_surname_is_unaffected(self) -> None:
        assert self.resolver().resolve("Saka").element_id == 3

    def test_shared_compound_tokens_stay_ambiguous(self) -> None:
        """Wider indexing must not buy recall at the cost of misattribution."""
        outcome = self.resolver().resolve("Silva")
        assert outcome.resolved is False
        assert sorted(outcome.candidates) == [4, 5]

    def test_a_club_hint_still_disambiguates(self) -> None:
        assert self.resolver().resolve("Silva", team_id=4).element_id == 5

    def test_forename_disambiguates_a_shared_compound(self) -> None:
        assert self.resolver().resolve("Bernardo Silva").element_id == 5


class TestSpecialistJudgement:
    """Weight by what a claim asserts, not only by how close the source is.

    Tiers measure distance from ground truth, which is right for facts: the club
    knows whether a player is injured, and a creator repeating it adds nothing.
    But for FPL-specific *judgement* - rotation risk, whether a role change
    matters - the specialist who thinks about nothing else is a better source
    than a match reporter who never considers it. A flat tier weight under-rated
    exactly the sources worth having.
    """

    def claim(self, tier: Tier, impact: str) -> Evidence:
        return make_evidence(tier=tier, impact=impact, claim="Rotation risk this week")

    def test_facts_still_rank_by_proximity(self) -> None:
        from arsenal.research.apply import authority_for

        official = authority_for(self.claim(Tier.FACT, "availability"))
        reporter = authority_for(self.claim(Tier.REPORTED, "availability"))
        creator = authority_for(self.claim(Tier.OPINION, "availability"))
        assert official > reporter > creator

    def test_judgement_lifts_the_specialist(self) -> None:
        """A creator on rotation risk counts for far more than on an injury."""
        from arsenal.research.apply import authority_for

        on_a_fact = authority_for(self.claim(Tier.OPINION, "availability"))
        on_judgement = authority_for(self.claim(Tier.OPINION, "minutes"))
        assert on_judgement > on_a_fact

    def test_judgement_never_overtakes_official_data(self) -> None:
        """An FPL analyst's read must not outrank a club announcement."""
        from arsenal.research.apply import authority_for

        creator = authority_for(self.claim(Tier.OPINION, "minutes"))
        official = authority_for(self.claim(Tier.FACT, "availability"))
        assert creator < official

    def test_tier_one_is_unaffected_by_impact(self) -> None:
        """Official data is already ground truth; specialism adds nothing to it."""
        from arsenal.research.apply import authority_for

        assert authority_for(self.claim(Tier.FACT, "availability")) == authority_for(
            self.claim(Tier.FACT, "minutes")
        )

    def test_creator_judgement_moves_a_forecast(self) -> None:
        """The whole point: a specialist's read on minutes now changes something."""
        report = build_report(
            [
                make_evidence(
                    tier=Tier.OPINION,
                    impact="minutes",
                    claim="Expected to be rested, will miss the match",
                )
            ],
            now=NOW,
            max_tier=Tier.OPINION,
        )
        assert report.adjustments[1].availability_multiplier < 1.0

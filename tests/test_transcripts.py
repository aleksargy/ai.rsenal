"""Transcript caching and description cleaning.

YouTube blocks IPs that scrape transcripts quickly, and it blocks cloud ranges
outright. The cache is what makes the source viable at all: a published video's
transcript never changes, so it should be fetched at most once, ever.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from arsenal.research import TranscriptCache, TranscriptFetcher, strip_boilerplate


class TestStripBoilerplate:
    """Creator descriptions are mostly affiliate links."""

    def test_removes_links_and_promos(self) -> None:
        description = (
            "Get your FPL Team rated for FREE https://bit.ly/4mwOxFQ\n"
            "Win your mini-league https://bit.ly/4vHkijC\n"
            "#AD\n"
            "━━━━━━━━━━━━━\n"
            "In this video I go through my final FPL thoughts ahead of Gameweek 4.\n"
            "Subscribe: https://bit.ly/SubLTFPL"
        )
        cleaned = strip_boilerplate(description)
        assert "final FPL thoughts" in cleaned
        assert "bit.ly" not in cleaned
        assert "#AD" not in cleaned
        assert "━" not in cleaned

    def test_keeps_content_only_descriptions_intact(self) -> None:
        text = "Team news: Haaland is fit, Foden is a doubt."
        assert strip_boilerplate(text) == text

    def test_an_entirely_promotional_description_becomes_empty(self) -> None:
        """Better empty than feeding the extractor a page of adverts."""
        assert strip_boilerplate("Subscribe here https://x.com\n#AD") == ""


class TestTranscriptCache:
    def test_round_trips(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        cache.put("abc123", "hello there")
        cached, text = cache.get("abc123")
        assert cached is True
        assert text == "hello there"

    def test_distinguishes_a_stored_miss_from_never_asked(self) -> None:
        """A video with captions disabled never acquires them.

        Re-asking every run is exactly the behaviour that gets an IP blocked, so
        misses are cached too — and the caller must be able to tell a cached
        "no captions" apart from "not looked up yet".
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache = TranscriptCache(Path(tmp))
            assert cache.get("never-asked") == (False, None)

            cache.put("no-captions", None)
            cached, text = cache.get("no-captions")
            assert cached is True
            assert text is None

    def test_survives_a_corrupt_entry(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
        assert cache.get("broken") == (False, None)

    def test_handles_url_safe_video_ids(self, tmp_path) -> None:
        """Video ids are base64url and contain '-' and '_'."""
        cache = TranscriptCache(tmp_path)
        cache.put("_31xZJl9-Ww", "text")
        assert cache.get("_31xZJl9-Ww")[1] == "text"


class TestProvenance:
    """Cached transcripts record when the video was published.

    Publication date is what makes a transcript relevant or not. A caption
    pulled this morning from a three-week-old video is three weeks stale, so
    fetch time is the wrong thing to reason about and the wrong thing to
    measure retention against.
    """

    def test_records_publication_and_fetch_times(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        published = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        cache.put("abc", "text", published_at=published, title="GW5", channel="LTFPL")

        entry = cache.entry("abc")
        assert entry is not None
        assert entry.published_at == published
        assert entry.fetched_at is not None
        assert entry.title == "GW5"
        assert entry.channel == "LTFPL"

    def test_age_is_measured_from_publication_not_fetch(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        published = datetime.now(UTC) - timedelta(days=30)
        cache.put("old", "text", published_at=published)

        # Fetched seconds ago, published a month ago. The transcript is a month old.
        assert cache.age_days("old") == pytest.approx(30, abs=0.01)

    def test_an_uncached_video_has_no_age(self, tmp_path) -> None:
        assert TranscriptCache(tmp_path).age_days("never-asked") is None


class TestRetention:
    """Committed transcripts have to be cleaned up or they accumulate forever.

    Creators publish several times a week across a 38-gameweek season. Without
    a retention window the archive grows to hundreds of files that no run will
    ever read, all of them committed to the repository.
    """

    def test_drops_what_has_aged_out_and_keeps_the_rest(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        now = datetime.now(UTC)
        cache.put("fresh", "recent", published_at=now - timedelta(days=2))
        cache.put("edge", "borderline", published_at=now - timedelta(days=20))
        cache.put("stale", "ancient", published_at=now - timedelta(days=60))

        removed = cache.prune(retention_days=21)

        assert removed == ["stale"]
        assert cache.get("fresh")[1] == "recent"
        assert cache.get("edge")[1] == "borderline"
        assert cache.get("stale") == (False, None)
        assert not (tmp_path / "stale.json").exists()

    def test_prunes_cached_misses_too(self, tmp_path) -> None:
        """A 'no captions' marker for an old video is just as dead as the text."""
        cache = TranscriptCache(tmp_path)
        cache.put("silent", None, published_at=datetime.now(UTC) - timedelta(days=90))
        assert cache.prune(retention_days=21) == ["silent"]

    def test_nothing_to_prune_is_not_an_error(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        cache.put("fresh", "text", published_at=datetime.now(UTC))
        assert cache.prune() == []

    def test_falls_back_to_file_time_for_entries_without_provenance(self, tmp_path) -> None:
        """Entries written before provenance was recorded still age out.

        Keeping them forever would mean the oldest files in the archive - the
        ones most certain to be stale - are the only ones immune to pruning.
        The fallback is sound because a transcript is only ever fetched from
        inside the search window, so fetch time is close to publication time.
        """
        import os

        cache = TranscriptCache(tmp_path)
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({"text": "old format"}), encoding="utf-8")
        ancient = (datetime.now(UTC) - timedelta(days=45)).timestamp()
        os.utime(legacy, (ancient, ancient))

        # Readable despite the old shape, and still subject to retention.
        assert cache.get("legacy") == (True, "old format")
        assert cache.prune(retention_days=21) == ["legacy"]

    def test_a_corrupt_entry_is_pruned_rather_than_kept_forever(self, tmp_path) -> None:
        import os

        cache = TranscriptCache(tmp_path)
        broken = tmp_path / "broken.json"
        broken.write_text("not json", encoding="utf-8")
        ancient = (datetime.now(UTC) - timedelta(days=45)).timestamp()
        os.utime(broken, (ancient, ancient))

        assert cache.prune(retention_days=21) == ["broken"]

    def test_ages_are_reported_newest_first(self, tmp_path) -> None:
        cache = TranscriptCache(tmp_path)
        now = datetime.now(UTC)
        cache.put("old", "t", published_at=now - timedelta(days=10))
        cache.put("new", "t", published_at=now - timedelta(days=1))
        cache.put("mid", "t", published_at=now - timedelta(days=5))

        assert [video_id for video_id, _ in cache.ages()] == ["new", "mid", "old"]

    def test_freshest_reports_the_newest_video_in_hours(self, tmp_path) -> None:
        """The scheduled run prints this, so a stale cache cannot pass as live."""
        fetcher = TranscriptFetcher(tmp_path, cache_only=True)
        fetcher.cache.put("a", "t", published_at=datetime.now(UTC) - timedelta(days=4))
        fetcher.cache.put("b", "t", published_at=datetime.now(UTC) - timedelta(days=9))

        assert fetcher.freshest == pytest.approx(96, abs=1)

    def test_freshest_is_none_on_an_empty_cache(self, tmp_path) -> None:
        assert TranscriptFetcher(tmp_path, cache_only=True).freshest is None


class TestCacheOnly:
    """A hosted runner reads the archive and never attempts a fetch.

    YouTube blocks cloud provider ranges outright, so an attempt cannot succeed
    - it can only waste time and teach YouTube that the IP is scraping.
    """

    def test_a_cache_miss_returns_nothing_without_fetching(self, tmp_path) -> None:
        fetcher = TranscriptFetcher(tmp_path, cache_only=True)
        assert fetcher.fetch("not-cached") is None
        assert fetcher.blocked is None
        assert not list(tmp_path.glob("*.json"))

    def test_still_serves_what_was_harvested_locally(self, tmp_path) -> None:
        fetcher = TranscriptFetcher(tmp_path, cache_only=True)
        fetcher.cache.put("committed", "captions from the local run")
        assert fetcher.fetch("committed") == "captions from the local run"

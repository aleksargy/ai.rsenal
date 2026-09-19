"""Community sources: Reddit and YouTube creators.

Both are **Tier 4**. They exist to surface claims worth verifying and to show what
the field is doing — never to move a number on their own. The tier rules enforce
that in code, so nothing here can quietly become evidence.

Both also need credentials, and both degrade to empty without them. An
unconfigured source and a failing one behave identically on purpose: they both
mean "no evidence from here", and the run reports it either way.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..config import REPO_ROOT
from .evidence import Tier
from .sources import USER_AGENT, Document
from .transcripts import TranscriptFetcher, strip_boilerplate

log = logging.getLogger(__name__)

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API = "https://oauth.reddit.com"

# Reddit rejects anonymous JSON requests with 403 — the old
# `reddit.com/r/x/hot.json` trick no longer works from anywhere it cannot
# identify. A registered script app is now the only reliable read path.
REDDIT_APP_URL = "https://www.reddit.com/prefs/apps"

# Deliberately NOT under data/cache/, which is gitignored as regenerable.
# Transcripts are neither regenerable from the cloud (YouTube blocks hosted IPs)
# nor secret (they are public captions), so they are committed to the repo. That
# is what lets a local machine harvest them and a scheduled cloud run use them.
DEFAULT_TRANSCRIPT_CACHE = REPO_ROOT / "data" / "transcripts"


class RedditClient:
    """Read-only Reddit access via an application-only OAuth token.

    A "script" app needs no user login: the client credentials grant returns a
    token that can read public listings. Create one at ``REDDIT_APP_URL``.
    """

    def __init__(self, client_id: str, client_secret: str, *, timeout: float = 20.0) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at: datetime | None = None

    @property
    def _token_valid(self) -> bool:
        return bool(self._token) and (
            self._expires_at is None or datetime.now(UTC) < self._expires_at
        )

    def _authenticate(self) -> None:
        response = httpx.post(
            REDDIT_TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = payload.get("expires_in", 3600)
        # Expire a minute early so a token cannot die between check and use.
        self._expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in) - 60)

    def hot(self, subreddit: str, *, limit: int = 25) -> list[dict]:
        if not self._token_valid:
            self._authenticate()
        response = httpx.get(
            f"{REDDIT_API}/r/{subreddit}/hot",
            params={"limit": limit},
            headers={
                "Authorization": f"Bearer {self._token}",
                "User-Agent": USER_AGENT,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return [
            child.get("data", {})
            for child in response.json().get("data", {}).get("children", [])
        ]


def fetch_reddit_documents(
    subreddits: list[str],
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    limit: int = 25,
    max_age_days: int = 5,
    min_score: int = 5,
) -> tuple[list[Document], str | None]:
    """Fetch recent r/FantasyPL posts as documents for claim extraction.

    Returns ``(documents, error)`` and never raises. Without credentials it
    returns a clear instruction rather than a failure — an unconfigured source is
    a setup gap, not a fault.

    Scout threads and daily discussion are the fastest route to late team news,
    frequently the highest-value input in the final hours before a deadline. They
    are still Tier 4: the rules will not let them move a forecast, only surface
    claims for verification at a higher tier.
    """
    if not client_id or not client_secret:
        return [], (
            "no Reddit credentials — anonymous access is blocked with 403. "
            f"Create a 'script' app at {REDDIT_APP_URL} and set REDDIT_CLIENT_ID "
            "and REDDIT_CLIENT_SECRET."
        )

    client = RedditClient(client_id, client_secret)
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    documents: list[Document] = []

    try:
        for subreddit in subreddits:
            for post in client.hot(subreddit, limit=limit):
                published = datetime.fromtimestamp(post.get("created_utc", 0), tz=UTC)
                if published < cutoff:
                    continue
                # A thread nobody upvoted is noise even by Tier 4 standards.
                if int(post.get("score", 0)) < min_score:
                    continue
                text = f"{post.get('title', '')}\n\n{post.get('selftext', '')}".strip()
                if len(text) < 40:
                    continue
                documents.append(
                    Document(
                        text=text[:6000],
                        url=f"https://www.reddit.com{post.get('permalink', '')}",
                        source_name=f"r/{subreddit}",
                        published_at=published,
                        tier=Tier.OPINION,
                    )
                )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("reddit fetch failed: %s", exc)
        return documents, str(exc)

    return documents, None


def fetch_youtube_documents(
    api_key: str | None,
    channel_ids: list[str],
    *,
    max_age_days: int = 5,
    per_channel: int = 3,
    with_transcripts: bool = True,
    cache_dir: Path | None = None,
    proxy: str | None = None,
    cache_only: bool = False,
) -> tuple[list[Document], str | None]:
    """Fetch recent creator videos, preferring transcripts over descriptions.

    Two endpoints are used, deliberately. ``search`` finds recent videos but
    truncates descriptions to ~120 characters; ``videos`` returns them in full.
    Even then a full description is mostly sponsorship copy, so boilerplate is
    stripped and the transcript is what actually carries claims.

    Transcripts are cached permanently and fetched slowly — see
    :mod:`arsenal.research.transcripts` for why that matters.
    """
    if not api_key or not channel_ids:
        return [], None

    published_after = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    documents: list[Document] = []
    fetcher = (
        TranscriptFetcher(
            cache_dir or DEFAULT_TRANSCRIPT_CACHE, proxy=proxy, cache_only=cache_only
        )
        if with_transcripts
        else None
    )

    try:
        with httpx.Client(timeout=20.0) as client:
            found: list[tuple[str, str, datetime]] = []
            for channel_id in channel_ids:
                response = client.get(
                    "https://www.googleapis.com/youtube/v3/search",
                    params={
                        "key": api_key,
                        "channelId": channel_id,
                        "part": "snippet",
                        "order": "date",
                        "maxResults": per_channel,
                        "type": "video",
                        "publishedAfter": published_after,
                    },
                )
                response.raise_for_status()
                for item in response.json().get("items", []):
                    video_id = item.get("id", {}).get("videoId")
                    snippet = item.get("snippet", {})
                    if not video_id:
                        continue
                    found.append(
                        (
                            video_id,
                            snippet.get("channelTitle", "YouTube"),
                            datetime.fromisoformat(
                                snippet["publishedAt"].replace("Z", "+00:00")
                            ),
                        )
                    )

            # One `videos` call covers up to 50 ids and returns full descriptions,
            # which `search` truncates.
            details: dict[str, dict] = {}
            if found:
                response = client.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params={
                        "key": api_key,
                        "id": ",".join(v for v, _, _ in found[:50]),
                        "part": "snippet",
                    },
                )
                response.raise_for_status()
                for item in response.json().get("items", []):
                    details[item["id"]] = item.get("snippet", {})

        for video_id, channel, published in found:
            snippet = details.get(video_id, {})
            title = snippet.get("title", "")
            description = strip_boilerplate(snippet.get("description", ""))

            body = f"{title}\n\n{description}".strip()
            if fetcher:
                transcript = fetcher.fetch(
                    video_id, published_at=published, title=title, channel=channel
                )
                if transcript:
                    # The transcript supersedes the description entirely — the
                    # latter is advertising with a sentence of content in it.
                    body = f"{title}\n\n{transcript}"

            if len(body) < 40:
                continue

            documents.append(
                Document(
                    text=body[:12000],
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    source_name=channel,
                    published_at=published,
                    tier=Tier.OPINION,
                )
            )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("youtube fetch failed: %s", exc)
        return documents, str(exc)

    return documents, (fetcher.blocked if fetcher else None)

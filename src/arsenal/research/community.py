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

import httpx

from .evidence import Tier
from .sources import USER_AGENT, Document

log = logging.getLogger(__name__)

REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API = "https://oauth.reddit.com"

# Reddit rejects anonymous JSON requests with 403 — the old
# `reddit.com/r/x/hot.json` trick no longer works from anywhere it cannot
# identify. A registered script app is now the only reliable read path.
REDDIT_APP_URL = "https://www.reddit.com/prefs/apps"


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
) -> tuple[list[Document], str | None]:
    """Fetch recent creator videos, with transcripts when available.

    The Data API returns titles and descriptions but **not** captions, so
    transcripts come from the optional ``youtube-transcript-api`` package. A
    description alone is thin — the actual claims are in what the creator says.

    Caveats worth keeping in view, all of which the tier rules already handle:
    creator content is stale on arrival (a Monday video is often obsolete by
    Friday's press conference), auto-captions mangle player names constantly, and
    creators are rewarded for bold calls rather than for calibration.
    """
    if not api_key or not channel_ids:
        return [], None

    published_after = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    documents: list[Document] = []
    try:
        with httpx.Client(timeout=20.0) as client:
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
                    snippet = item.get("snippet", {})
                    video_id = item.get("id", {}).get("videoId")
                    if not video_id:
                        continue

                    body = f"{snippet.get('title', '')}\n\n{snippet.get('description', '')}"
                    if with_transcripts:
                        transcript = _transcript(video_id)
                        if transcript:
                            body = f"{snippet.get('title', '')}\n\n{transcript}"

                    documents.append(
                        Document(
                            text=body[:8000],
                            url=f"https://www.youtube.com/watch?v={video_id}",
                            source_name=snippet.get("channelTitle", "YouTube"),
                            published_at=datetime.fromisoformat(
                                snippet["publishedAt"].replace("Z", "+00:00")
                            ),
                            tier=Tier.OPINION,
                        )
                    )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("youtube fetch failed: %s", exc)
        return documents, str(exc)

    return documents, None


def _transcript(video_id: str) -> str | None:
    """Fetch a video transcript, if the optional dependency is installed.

    Returns None rather than raising: a video without captions is common, and it
    should cost that video's transcript and nothing else.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-GB", "en-US"])
        return " ".join(chunk.text for chunk in fetched)
    except Exception as exc:  # the library raises many distinct errors
        log.debug("no transcript for %s: %s", video_id, exc)
        return None

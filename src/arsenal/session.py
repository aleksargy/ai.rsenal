"""Getting an authenticated client, with a credential guaranteed fresh.

One place owns the whole dance: load the stored session, refresh the access token
if it is near expiry, persist any rotated refresh token, and hand back a client
that will actually authenticate.

It lives in one function because the alternative is every caller remembering to
refresh — and the one that forgets fails at a deadline, hours after anyone could
have noticed.
"""

from __future__ import annotations

import logging

from .config import REPO_ROOT, Config, update_env_file
from .fpl.auth import Session
from .fpl.client import FPLClient
from .fpl.oidc import OidcTokens, TokenError, ensure_fresh

log = logging.getLogger(__name__)

SESSION_PATH = REPO_ROOT / "session.json"


class NoSession(RuntimeError):
    """No usable credential is available.

    Distinct from an authentication *failure*: this means nothing was configured,
    which is a setup problem, not an expiry problem.
    """


def load_session(config: Config) -> Session | None:
    """Load the stored session, preferring the environment over the file.

    ``FPL_SESSION_JSON`` wins because that is what CI provides, and a stale local
    ``session.json`` silently shadowing it would be a miserable thing to debug.
    """
    if config.secrets.has_session:
        return Session(
            cookies=config.secrets.session_cookies,
            tokens=OidcTokens.from_dict(config.secrets.oidc),
        )
    return Session.load(SESSION_PATH)


def refresh_if_needed(session: Session, *, persist: bool = True) -> tuple[Session, bool]:
    """Refresh the access token when it is near expiry.

    Returns ``(session, refreshed)``. Persisting is the default and matters more
    than it looks: PingOne may rotate the refresh token on every use, and a
    rotated token that is not written back destroys the only durable credential
    the agent has.
    """
    if not session.tokens.needs_refresh:
        return session, False
    if not session.tokens.can_refresh:
        log.warning(
            "access token needs refreshing but no refresh token is stored; "
            "re-capture with `arsenal auth attach`"
        )
        return session, False

    tokens, refreshed = ensure_fresh(session.tokens)
    if not refreshed:
        return session, False

    session.tokens = tokens
    if persist:
        session.save(SESSION_PATH)
        update_env_file({"FPL_SESSION_JSON": session.to_env_value()})

        # On a hosted runner the filesystem is discarded when the job ends, so
        # the rotated token has to go back to the repository secret or the next
        # run authenticates with one the provider has already revoked. Locally
        # this is a no-op — .env is the durable store.
        from .github_secrets import persist_session

        problem = persist_session(session.to_env_value())
        if problem:
            log.error(
                "the refresh token rotated but could not be saved to the "
                "repository secret: %s — the next scheduled run will fail to "
                "authenticate",
                problem,
            )
    return session, True


def authenticated_client(
    config: Config, *, gameweek: int | None = None, required: bool = True
) -> FPLClient:
    """An FPL client carrying a working credential.

    FPL authenticates with an OIDC **bearer token**, not cookies — replaying
    cookies returns 403. Cookies are still sent because they cost nothing and may
    carry Cloudflare clearance, but the token is what does the work.

    ``required=False`` returns an unauthenticated client when no session exists,
    which is correct for read-only commands: every public endpoint works without
    one, and refusing to run them would be gratuitous.
    """
    session = load_session(config)

    if session is None or not session.has_credentials:
        if required:
            raise NoSession(
                "no FPL session configured. Capture one with `arsenal auth attach`."
            )
        return _client_for(config, None, gameweek)

    try:
        session, refreshed = refresh_if_needed(session)
        if refreshed:
            log.info("refreshed the access token before use")
    except TokenError as exc:
        if required:
            raise NoSession(f"could not refresh the access token: {exc}") from exc
        log.warning("token refresh failed, continuing unauthenticated: %s", exc)
        return _client_for(config, None, gameweek)

    return _client_for(config, session, gameweek)


def _client_for(config: Config, session: Session | None, gameweek: int | None) -> FPLClient:
    raw_dir = config.run_dir(gameweek) / "raw" if gameweek is not None else None
    return FPLClient(
        config.cache_dir,
        session_cookies=(session.cookies if session else None) or None,
        bearer_token=session.access_token if session else None,
        raw_dir=raw_dir,
    )

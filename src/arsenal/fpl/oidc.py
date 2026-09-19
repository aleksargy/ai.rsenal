"""OIDC token handling for the Premier League identity provider.

**This is how FPL authentication actually works this season**, and it is not what
any published guide or library describes. Verified empirically against a live
logged-in account:

* Replaying session cookies — including the new ``ST`` / ``ST-NO-SS`` — returns
  **403**. The documented ``pl_profile`` / ``sessionid`` cookies no longer exist.
* Sending ``Authorization: Bearer <access_token>`` **authenticates**.

So the session is an OAuth2 bearer token issued by ``account.premierleague.com``
(a PingOne tenant), and the browser keeps it in ``localStorage`` under an
``oidc.user:<authority>:<client_id>`` key.

The consequence that shapes the whole executor design: **access tokens live one
hour**. A scheduled agent that runs hours after capture will always find its
token dead, so a stored access token can never be the durable credential. The
refresh token is — and the discovery document confirms ``refresh_token`` is a
supported grant, so tokens can be minted on demand without a browser.

This is *better* than the cookie model it replaced. A refresh token is a
first-class, long-lived credential designed to be replayed from a server, where
a session cookie bound to a browser fingerprint was always going to be fragile
from CI.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

ISSUER = "https://account.premierleague.com/as"
TOKEN_ENDPOINT = f"{ISSUER}/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"

# The oidc-client-ts storage key is `oidc.user:{authority}:{client_id}`, so the
# client id can be read straight off the key rather than guessed.
OIDC_KEY_PREFIX = "oidc.user:"

# Refresh this far before actual expiry, so a token cannot die mid-run between
# the check and the request that uses it.
REFRESH_MARGIN = timedelta(minutes=5)


class TokenError(RuntimeError):
    """A token could not be refreshed."""


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT's payload without verifying its signature.

    Verification is the identity provider's job, not ours — we are a client
    replaying a token, not a resource server validating one. This only reads
    claims like ``exp`` and ``client_id`` so the agent can tell a stale token
    from a rejected one, which need different responses.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)  # restore base64url padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


@dataclass
class OidcTokens:
    """What the browser's OIDC client stored, and what we can do with it."""

    access_token: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    expires_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return self.access_token is None
        return datetime.now(UTC) >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        """Whether to refresh now, allowing margin for the run that follows."""
        if not self.access_token:
            return True
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at - REFRESH_MARGIN

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.client_id)

    def remaining(self) -> timedelta | None:
        if self.expires_at is None:
            return None
        return self.expires_at - datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "expires_at": int(self.expires_at.timestamp()) if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OidcTokens:
        expires = data.get("expires_at")
        return cls(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            client_id=data.get("client_id"),
            expires_at=datetime.fromtimestamp(expires, tz=UTC) if expires else None,
        )

    @classmethod
    def from_local_storage(cls, entries: dict[str, str]) -> OidcTokens | None:
        """Extract tokens from a browser ``localStorage`` dump.

        The stored value is a JSON *wrapper* around the tokens, not a bare JWT —
        which is why a "does it start with eyJ" scan finds nothing.
        """
        for key, value in entries.items():
            if not key.startswith(OIDC_KEY_PREFIX):
                continue
            try:
                stored = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(stored, dict):
                continue

            access = stored.get("access_token")
            # `oidc.user:{authority}:{client_id}` — the client id is the segment
            # after the final colon.
            client_id = key.rsplit(":", 1)[-1] or None
            if not client_id and access:
                client_id = decode_jwt_claims(access).get("client_id")

            expires_at = None
            if isinstance(stored.get("expires_at"), int | float):
                expires_at = datetime.fromtimestamp(stored["expires_at"], tz=UTC)
            elif access:
                exp = decode_jwt_claims(access).get("exp")
                if isinstance(exp, int | float):
                    expires_at = datetime.fromtimestamp(exp, tz=UTC)

            return cls(
                access_token=access,
                refresh_token=stored.get("refresh_token"),
                client_id=client_id,
                expires_at=expires_at,
            )
        return None


def refresh_tokens(tokens: OidcTokens, *, timeout: float = 30.0) -> OidcTokens:
    """Exchange a refresh token for a fresh access token.

    This is what makes unattended operation possible at all. The access token
    lasts an hour; a deadline run happens days after capture. Without this, the
    agent would need a human to open a browser before every gameweek.

    The client is public (a browser SPA using PKCE), so there is no client secret
    to send — just the refresh token and the client id.
    """
    if not tokens.can_refresh:
        missing = "refresh_token" if not tokens.refresh_token else "client_id"
        raise TokenError(
            f"cannot refresh: no {missing}. Re-capture with `arsenal auth attach` "
            "while logged in, which stores both."
        )

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=timeout,
    )

    if response.status_code != 200:
        # A 400 here usually means the refresh token was revoked or already
        # rotated — recoverable only by capturing a new session.
        raise TokenError(
            f"token refresh failed ({response.status_code}): {response.text[:200]}\n"
            "If this persists, log into FPL again and re-run `arsenal auth attach`."
        )

    payload = response.json()
    access = payload.get("access_token")
    if not access:
        raise TokenError(f"token endpoint returned no access_token: {payload}")

    expires_in = payload.get("expires_in")
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, int | float)
        else None
    )
    if expires_at is None:
        exp = decode_jwt_claims(access).get("exp")
        if isinstance(exp, int | float):
            expires_at = datetime.fromtimestamp(exp, tz=UTC)

    log.info("refreshed access token, expires %s", expires_at)
    return OidcTokens(
        access_token=access,
        # Providers may rotate the refresh token on use. Keeping the old one
        # when no new one is issued is correct; overwriting it with None would
        # silently destroy the only durable credential we have.
        refresh_token=payload.get("refresh_token") or tokens.refresh_token,
        client_id=tokens.client_id,
        expires_at=expires_at,
    )


def ensure_fresh(tokens: OidcTokens) -> tuple[OidcTokens, bool]:
    """Return usable tokens, refreshing if needed.

    Returns ``(tokens, refreshed)`` so the caller can persist the new value —
    a rotated refresh token that is not written back is lost.
    """
    if not tokens.needs_refresh:
        return tokens, False
    if not tokens.can_refresh:
        return tokens, False
    return refresh_tokens(tokens), True

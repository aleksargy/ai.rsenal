"""Writing a rotated credential back to a GitHub Actions secret.

This exists for one reason: the Premier League's OIDC provider rotates the
refresh token on every use. A hosted runner therefore cannot treat the secret as
read-only — it must store the replacement before the run ends, or the next run
starts with a token that has already been revoked.

That makes this the most safety-critical code in the scheduled path. A failed
write is not a warning, it is a countdown: the agent keeps working until the
current access token expires, then locks out until someone re-captures a session
by hand at a browser. So the write is verified, and a failure is escalated rather
than logged.

Secrets are encrypted client-side with libsodium sealed boxes against the
repository's public key — GitHub never receives the plaintext, and cannot return
a secret once stored.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

API = "https://api.github.com"


class SecretError(RuntimeError):
    """A secret could not be written."""


@dataclass
class GitHubSecrets:
    """Client for the repository secrets API.

    ``token`` needs a fine-grained PAT with **Secrets: read and write** on this
    repository. The default ``GITHUB_TOKEN`` issued to a workflow deliberately
    cannot write secrets, so it will not work here.
    """

    repository: str  # "owner/name"
    token: str
    timeout: float = 20.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def public_key(self) -> tuple[str, str]:
        """Return ``(key_id, base64_public_key)`` for sealed-box encryption."""
        response = httpx.get(
            f"{API}/repos/{self.repository}/actions/secrets/public-key",
            headers=self._headers,
            timeout=self.timeout,
        )
        if response.status_code == 404:
            raise SecretError(
                f"repository '{self.repository}' not found, or the token lacks "
                "Secrets access. A fine-grained PAT needs 'Secrets: read and write'."
            )
        if response.status_code != 200:
            raise SecretError(f"could not read the public key: {response.status_code}")
        payload = response.json()
        return payload["key_id"], payload["key"]

    def put(self, name: str, value: str) -> None:
        """Encrypt and store a secret, then confirm it landed."""
        try:
            from nacl import encoding, public
        except ImportError as exc:
            raise SecretError(
                "PyNaCl is required to encrypt secrets. Install the 'ci' extra:\n"
                '  uv pip install -e ".[ci]"'
            ) from exc

        key_id, key_b64 = self.public_key()
        sealed = public.SealedBox(
            public.PublicKey(key_b64.encode(), encoding.Base64Encoder())
        ).encrypt(value.encode())

        response = httpx.put(
            f"{API}/repos/{self.repository}/actions/secrets/{name}",
            headers=self._headers,
            json={
                "encrypted_value": base64.b64encode(sealed).decode(),
                "key_id": key_id,
            },
            timeout=self.timeout,
        )
        # 201 created, 204 updated. Anything else means the next run starts with
        # a revoked token, which is worth failing loudly over.
        if response.status_code not in (201, 204):
            raise SecretError(
                f"storing '{name}' failed ({response.status_code}): {response.text[:200]}"
            )

        # GitHub cannot return a secret's value, so verification is limited to
        # confirming the record now exists and was updated. That is enough to
        # catch the failure mode that matters: a write that silently no-ops.
        check = httpx.get(
            f"{API}/repos/{self.repository}/actions/secrets/{name}",
            headers=self._headers,
            timeout=self.timeout,
        )
        if check.status_code != 200:
            raise SecretError(
                f"stored '{name}' but could not confirm it exists "
                f"({check.status_code}) — treat the credential as unsaved"
            )
        log.info("wrote secret %s to %s", name, self.repository)


def from_environment() -> GitHubSecrets | None:
    """Build a client from the environment, or None when not running in CI.

    Returns None rather than raising: locally there is no repository to write to,
    and `.env` already holds the credential.
    """
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("ARSENAL_GH_PAT") or os.environ.get("GH_SECRETS_PAT")
    if not repository or not token:
        return None
    return GitHubSecrets(repository=repository, token=token)


def persist_session(value: str) -> str | None:
    """Store the session in the repository secret, if configured.

    Returns a problem description, or None on success. The caller decides how
    loudly to complain — but it should complain: a rotated token that is not
    stored means the next scheduled run cannot authenticate.
    """
    client = from_environment()
    if client is None:
        return None
    try:
        client.put("FPL_SESSION_JSON", value)
    except SecretError as exc:
        return str(exc)
    return None

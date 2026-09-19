"""Session capture and validation.

There is no scriptable FPL login. ``users.premierleague.com`` no longer resolves,
and ``account.premierleague.com`` returns 403 to any non-browser client. So
authenticated state has to be harvested from a real browser and replayed.

Three ways to do that, in descending order of how well they survive contact with
identity providers:

* **Attach** (:func:`session_from_cdp`) — read cookies from a Chrome you are
  already logged into. Nothing is automated except reading the result, so there
  is no login for anyone to block. The only option that works with Google SSO.
* **Manual paste** (:func:`session_from_pasted_cookies`) — copy the cookies out
  of DevTools. No extra install, works everywhere.
* **Browser login** (:func:`login_with_browser`) — Playwright drives a real
  Chrome and you log in by hand. Fine for FPL's own email/password login;
  **blocked by Google SSO**, which detects remote-controlled browsers.

Either way the result is the same shape, and either way it eventually expires.
Expiry is a normal operating condition with a defined recovery path, not an
exception — see the ``fpl-api`` skill.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .oidc import OidcTokens

log = logging.getLogger(__name__)

# The FPL front end is a single-page app served from here; the session cookies
# are scoped to the parent domain.
FPL_URL = "https://fantasy.premierleague.com/"
COOKIE_DOMAIN = "premierleague.com"

# Cookies that have at some point carried an FPL session. A **hint for
# diagnostics only** — never a gate.
#
# `pl_profile` and `sessionid` are what every FPL guide and library still
# documents, and they no longer exist: a confirmed logged-in browser this season
# carries neither. `ST` / `ST-NO-SS` are what appeared in their place. Since the
# naming has already changed once and is documented nowhere, treating any of
# these as required would just re-create the same failure next season.
#
# Every premierleague.com cookie is captured regardless, and the only real test
# of a session is whether `my-team/` answers.
SESSION_COOKIE_HINTS = ("ST", "ST-NO-SS", "pl_profile", "sessionid")

# Where the OIDC client parks its tokens. FPL authenticates through
# `account.premierleague.com`, an OpenID Connect provider, and the browser keeps
# the resulting tokens under a key of this shape. The value is a JSON object
# wrapping `id_token`, `access_token`, `refresh_token` and `expires_at` — which
# is why a naive "does it start with eyJ" JWT check misses it entirely.
OIDC_KEY_PREFIX = "oidc.user:"

DEFAULT_CDP_ENDPOINT = "http://localhost:9222"

PLAYWRIGHT_MISSING = (
    "Playwright is not installed. Either run:\n"
    '  uv pip install -e ".[browser]" && uv run playwright install chromium\n'
    "or paste cookies manually with `arsenal auth paste`."
)


@dataclass
class Session:
    """A captured browser session, in a form the HTTP client can replay."""

    cookies: dict[str, str] = field(default_factory=dict)
    storage_state: dict[str, Any] | None = None
    tokens: OidcTokens = field(default_factory=OidcTokens)
    """The OIDC tokens. **This is the credential that actually works.**

    Verified against a live account: replaying cookies returns 403, sending
    ``Authorization: Bearer <access_token>`` authenticates.
    """

    @property
    def access_token(self) -> str | None:
        return self.tokens.access_token

    @property
    def has_credentials(self) -> bool:
        """Whether there is anything here worth trying against the API."""
        return bool(self.cookies) or bool(self.tokens.access_token)

    @property
    def recognised_cookies(self) -> list[str]:
        return [name for name in SESSION_COOKIE_HINTS if name in self.cookies]

    def diagnose(self) -> str:
        """A human-readable read on what this session holds.

        Descriptive rather than a verdict. Cookie names changed this season with
        no announcement, so the only honest verdict comes from calling the API.
        """
        parts = []
        if self.cookies:
            known = self.recognised_cookies
            parts.append(
                f"{len(self.cookies)} cookies"
                + (f" (recognised: {', '.join(known)})" if known else " (none recognised)")
            )
        if self.tokens.access_token:
            detail = "an OIDC access token"
            if self.tokens.refresh_token:
                detail += " with a refresh token"
            remaining = self.tokens.remaining()
            if remaining is not None:
                minutes = remaining.total_seconds() / 60
                detail += " (expired)" if minutes <= 0 else f" (valid {minutes:.0f}m)"
            parts.append(detail)
        if not parts:
            return "nothing captured"
        return "captured " + " and ".join(parts)

    def to_env_value(self) -> str:
        """Serialise for ``FPL_SESSION_JSON``.

        The full storage state is preferred when available: it round-trips into
        Playwright for the browser-replay executor layer, where a bare cookie map
        would not.
        """
        if self.storage_state:
            payload: Any = dict(self.storage_state)
        else:
            payload = {"cookies": self.cookies}
        if self.tokens.access_token or self.tokens.refresh_token:
            payload["oidc"] = self.tokens.to_dict()
        return json.dumps(payload, separators=(",", ":"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_env_value(), encoding="utf-8")
        # The file is a live credential. Best-effort lockdown; Windows ACLs make
        # chmod a no-op there, so this is defence in depth rather than a promise.
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> Session | None:
        if not path.exists():
            return None
        try:
            return cls.from_payload(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read session at %s: %s", path, exc)
            return None

    @classmethod
    def from_payload(cls, payload: Any) -> Session:
        """Accept a Playwright ``storage_state``, a cookie list, or a flat map."""
        if not isinstance(payload, dict):
            raise ValueError("session payload must be a JSON object")

        if isinstance(payload.get("oidc"), dict):
            tokens = OidcTokens.from_dict(payload["oidc"])
        elif payload.get("access_token"):
            # Older shape, captured before refresh tokens were stored.
            tokens = OidcTokens(access_token=str(payload["access_token"]))
        else:
            tokens = OidcTokens()

        raw_cookies = payload.get("cookies")

        if isinstance(raw_cookies, list):
            cookies = {
                entry["name"]: entry["value"]
                for entry in raw_cookies
                if COOKIE_DOMAIN in entry.get("domain", "")
            }
            state = payload if "origins" in payload else None
            return cls(cookies=cookies, storage_state=state, tokens=tokens)

        if isinstance(raw_cookies, dict):
            return cls(cookies={str(k): str(v) for k, v in raw_cookies.items()}, tokens=tokens)

        # A bare mapping of cookie name to value.
        reserved = {"access_token", "oidc", "origins"}
        flat = {str(k): str(v) for k, v in payload.items() if k not in reserved}
        return cls(cookies=flat, tokens=tokens)

    def token_expiry(self) -> datetime | None:
        return self.tokens.expires_at

    @property
    def is_token_expired(self) -> bool:
        return self.tokens.is_expired

    @staticmethod
    def token_from_local_storage(entries: dict[str, str]) -> str | None:
        """Access token only. Prefer ``OidcTokens.from_local_storage``, which
        also returns the refresh token the agent needs for unattended runs."""
        tokens = OidcTokens.from_local_storage(entries)
        return tokens.access_token if tokens else None


def session_from_pasted_cookies(raw: str) -> Session:
    """Parse cookies copied out of a browser's developer tools.

    Deliberately forgiving about format, because this is a manual step done under
    mild frustration. All of these work::

        pl_profile=abc; sessionid=def
        pl_profile abc
        sessionid    def
        {"pl_profile": "abc", "sessionid": "def"}
    """
    raw = raw.strip()
    if not raw:
        return Session()

    if raw.startswith("{"):
        return Session.from_payload(json.loads(raw))

    cookies: dict[str, str] = {}
    # A single line of "a=1; b=2", or one name/value pair per line.
    chunks = raw.split(";") if "=" in raw and ";" in raw else raw.splitlines()
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            # Partition, not split: `pl_profile` is base64 and often ends in '='.
            name, _, value = chunk.partition("=")
        else:
            parts = chunk.split(None, 1)
            if len(parts) != 2:
                continue
            name, value = parts
        cookies[name.strip()] = value.strip().strip('"')

    return Session(cookies=cookies)


def _playwright():
    """Import Playwright, with an actionable message when it is absent."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(PLAYWRIGHT_MISSING) from exc
    return sync_playwright


def session_from_cdp(endpoint: str = DEFAULT_CDP_ENDPOINT) -> Session:
    """Read cookies from a Chrome you are already logged into.

    This sidesteps the login problem rather than trying to beat it. Google blocks
    OAuth sign-in from automation-controlled browsers ("This browser or app may
    not be secure") and hardens against every workaround, so driving a login
    through Playwright is a losing fight for any account using Google SSO.

    Attaching instead means the login already happened, in your own browser, by
    hand. Nothing is automated except reading the result.

    Start Chrome with remote debugging first — closing all existing windows
    first, since Chrome otherwise hands the request to the running instance and
    silently ignores the flag::

        chrome.exe --remote-debugging-port=9222

    Then log into FPL in that window and attach.
    """
    sync_playwright = _playwright()

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            raise RuntimeError(
                f"could not attach to a browser at {endpoint}.\n"
                "Close every Chrome window, then start it with:\n"
                "  chrome.exe --remote-debugging-port=9222\n"
                "log into FPL in that window, and try again."
            ) from exc

        cookies: dict[str, str] = {}
        tokens: OidcTokens | None = None
        for context in browser.contexts:
            for cookie in context.cookies():
                if COOKIE_DOMAIN in cookie.get("domain", ""):
                    cookies[cookie["name"]] = cookie["value"]

            # The OIDC tokens live in localStorage, which is readable only from
            # an open page on the origin.
            for page in context.pages:
                if tokens or COOKIE_DOMAIN not in page.url:
                    continue
                try:
                    entries = page.evaluate(
                        "() => Object.fromEntries(Object.entries(window.localStorage))"
                    )
                except Exception:  # a page can be mid-navigation
                    continue
                tokens = OidcTokens.from_local_storage(entries or {})

        browser.close()

    session = Session(cookies=cookies, tokens=tokens or OidcTokens())
    if not session.has_credentials:
        raise RuntimeError(
            "attached, but found no premierleague.com cookies or tokens at all.\n"
            "Open https://fantasy.premierleague.com/my-team in that browser, "
            "confirm your squad is visible, and try again."
        )
    # Deliberately no cookie-name gate. `pl_profile` and `sessionid` are gone
    # this season and `ST`/`ST-NO-SS` replaced them with no announcement, so
    # only the API can say whether these credentials actually work.
    return session


def inspect_browser(endpoint: str = DEFAULT_CDP_ENDPOINT) -> dict[str, Any]:
    """Report what authentication state a logged-in browser actually holds.

    Built because the documented model stopped matching reality: a confirmed
    logged-in browser showed no ``pl_profile`` cookie at all. Rather than guess
    at replacement cookie names, this enumerates every place a session could
    live — cookies, localStorage, sessionStorage — so the real mechanism can be
    identified from evidence.

    **Values are redacted.** Only names, lengths and a short prefix are returned,
    which is enough to recognise a JWT or a session id without putting a live
    credential on screen.
    """
    sync_playwright = _playwright()

    def redact(value: str) -> str:
        if len(value) <= 8:
            return f"<{len(value)} chars>"
        return f"{value[:6]}… <{len(value)} chars>"

    report: dict[str, Any] = {
        "cookies": {},
        "local_storage": {},
        "session_storage": {},
        "pages": [],
        "looks_like_jwt": [],
    }

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            raise RuntimeError(
                f"could not attach to a browser at {endpoint}.\n"
                "Close every window of that browser, restart it with\n"
                "  --remote-debugging-port=9222\n"
                "log into FPL, and try again."
            ) from exc

        for context in browser.contexts:
            for cookie in context.cookies():
                if COOKIE_DOMAIN in cookie.get("domain", ""):
                    report["cookies"][cookie["name"]] = {
                        "domain": cookie.get("domain"),
                        "http_only": cookie.get("httpOnly"),
                        "value": redact(cookie.get("value", "")),
                    }

            for page in context.pages:
                url = page.url
                report["pages"].append(url)
                if COOKIE_DOMAIN not in url:
                    continue
                for store in ("localStorage", "sessionStorage"):
                    try:
                        entries = page.evaluate(
                            f"() => Object.fromEntries(Object.entries(window.{store}))"
                        )
                    except Exception:  # a page can be mid-navigation
                        continue
                    key = "local_storage" if store == "localStorage" else "session_storage"
                    for name, value in (entries or {}).items():
                        text = str(value)
                        report[key][name] = redact(text)
                        # A JWT is three base64 segments separated by dots and
                        # starts with the standard header. Spotting one tells us
                        # the session is bearer-token based, not cookie based.
                        if text.startswith("eyJ") and text.count(".") >= 2:
                            report["looks_like_jwt"].append(f"{store}.{name}")

        browser.close()

    return report


def login_with_browser(
    *,
    timeout_seconds: int = 300,
    headless: bool = False,
    profile_dir: Path | None = None,
) -> Session:
    """Open a browser, wait for a manual login, and capture the session.

    Uses your **installed Chrome** rather than Playwright's bundled Chromium, and
    suppresses the flags that set ``navigator.webdriver``. That is enough for
    FPL's own email/password login.

    It is **not** enough for Google SSO. Google detects remote-controlled
    browsers regardless and returns "This browser or app may not be secure". If
    your FPL account signs in through Google, use :func:`session_from_cdp` or
    :func:`session_from_pasted_cookies` — neither automates a login at all.

    A persistent profile directory keeps the login between runs, so you only go
    through it once.
    """
    sync_playwright = _playwright()

    profile = profile_dir or (Path.home() / ".arsenal" / "browser-profile")
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        # `--disable-blink-features=AutomationControlled` clears the
        # `navigator.webdriver` flag, and dropping `--enable-automation` removes
        # the "controlled by automated test software" infobar. Together they get
        # past FPL's own login. They do not get past Google's.
        launch: dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
            "ignore_default_args": ["--enable-automation"],
        }
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", **launch
            )
        except Exception:
            # No system Chrome — fall back to bundled Chromium. More likely to be
            # flagged, but better than failing outright.
            log.info("system Chrome unavailable; falling back to bundled Chromium")
            context = playwright.chromium.launch_persistent_context(str(profile), **launch)

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(FPL_URL)

        log.info("waiting for login at %s", FPL_URL)
        # The session cookie appearing is the signal that login succeeded — far
        # more reliable than watching for a URL or a DOM element, both of which
        # change whenever the front end is redesigned.
        elapsed = 0
        step = 2000
        while elapsed < timeout_seconds * 1000:
            names = {c["name"] for c in context.cookies()}
            # Any recognised session cookie means login completed. `any`, not
            # `all`: the set changes between seasons, and waiting for a specific
            # combination would hang forever the next time FPL renames one.
            if any(name in names for name in SESSION_COOKIE_HINTS):
                break
            page.wait_for_timeout(step)
            elapsed += step

        state = context.storage_state()
        context.close()

    session = Session.from_payload(state)
    if not session.has_credentials:
        raise RuntimeError(
            "login did not complete — no premierleague.com cookies or tokens were "
            "captured.\n"
            "If you saw 'This browser or app may not be secure', that is Google "
            "blocking sign-in from an automated browser. Use `arsenal auth attach` "
            "or `arsenal auth paste` instead — neither automates a login."
        )
    return session

"""Telegram notifications.

The agent is invisible when it works. A scheduled run that quietly does the
right thing every gameweek looks identical to one that has been silently broken
since October — so the notification is not a nicety, it is the only evidence the
system is alive.

Which means it has to report failure at least as clearly as success. A summary
that only arrives when everything went well teaches you to assume silence is
fine, and silence is exactly what a broken agent produces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Telegram rejects messages over 4096 characters. Splitting mid-message is worse
# than trimming: a summary cut in half reads as a bug.
MAX_MESSAGE = 4000


class NotifyError(RuntimeError):
    """A notification could not be delivered."""


@dataclass
class Summary:
    """What one run did, in a form that can be rendered to a message."""

    gameweek: int
    deadline: str
    headline: str
    transfers: list[str] = field(default_factory=list)
    captain: str = ""
    chip: str | None = None
    expected_points: float = 0.0
    horizon_points: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    submitted: bool = False

    def to_markdown(self) -> str:
        """Render for Telegram's MarkdownV2-lite (`parse_mode=Markdown`).

        Deliberately plain. Escaping rules for the richer modes are fiddly enough
        that a player's name with a bracket in it could fail the whole send, and
        a notification that does not arrive is worse than one that looks dull.
        """
        lines = [f"*GW{self.gameweek}* — {self.headline}", f"_deadline {self.deadline}_", ""]

        if self.transfers:
            lines.append("*Transfers*")
            lines.extend(f"  {line}" for line in self.transfers)
        else:
            lines.append("*Transfers* — none; nothing clears the cost of making one")
        lines.append("")

        if self.captain:
            lines.append(f"*Captain* {self.captain}")
        if self.chip:
            lines.append(f"*Chip* {self.chip}")
        lines.append(
            f"*Expected* {self.expected_points:.1f} this GW, "
            f"{self.horizon_points:.1f} over the horizon"
        )

        if self.reasons:
            lines += ["", "*Why*"]
            lines.extend(f"  {reason}" for reason in self.reasons[:8])

        # Warnings last and unmissable. Anything that degraded belongs in front
        # of the reader, not buried in a log they will never open.
        if self.warnings:
            lines += ["", "⚠️ *Problems*"]
            lines.extend(f"  {warning}" for warning in self.warnings[:8])

        lines += [
            "",
            "✅ submitted to FPL" if self.submitted else "🔍 dry run — nothing submitted",
        ]

        text = "\n".join(lines)
        if len(text) > MAX_MESSAGE:
            text = text[: MAX_MESSAGE - 40].rstrip() + "\n\n_…trimmed_"
        return text


class Telegram:
    """Minimal Telegram bot client — just enough to deliver a summary."""

    def __init__(self, token: str, chat_id: str, *, timeout: float = 20.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str, *, markdown: bool = True) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if markdown:
            payload["parse_mode"] = "Markdown"

        response = httpx.post(
            f"{API_BASE}/bot{self.token}/sendMessage", json=payload, timeout=self.timeout
        )

        if response.status_code == 400 and markdown:
            # Almost always a stray markdown character in a player's name. The
            # content matters more than the formatting, so resend it plain.
            log.info("markdown rejected, resending as plain text")
            self.send(text, markdown=False)
            return

        if response.status_code != 200:
            raise NotifyError(
                f"telegram returned {response.status_code}: {response.text[:200]}"
            )

    def check(self) -> str:
        """Confirm the token works and return the bot's name."""
        response = httpx.get(f"{API_BASE}/bot{self.token}/getMe", timeout=self.timeout)
        if response.status_code != 200:
            raise NotifyError(
                f"token rejected ({response.status_code}). Check TELEGRAM_BOT_TOKEN."
            )
        return response.json().get("result", {}).get("username", "?")


def build(token: str | None, chat_id: str | None) -> Telegram | None:
    """Construct a client, or None when notifications are not configured.

    Returning None is a normal state: the pipeline runs fine without
    notifications, it just cannot tell you about it.
    """
    if not token or not chat_id:
        return None
    return Telegram(token, chat_id)

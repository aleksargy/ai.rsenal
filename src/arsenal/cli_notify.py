"""``arsenal notify`` — send a message, or prove the channel works."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from .config import Config
from .notify import NotifyError, build

console = Console()


def notify(
    message: Annotated[str | None, typer.Argument(help="Message to send")] = None,
    failure: Annotated[
        str | None, typer.Option("--failure", help="Send as a failure alert")
    ] = None,
    test: Annotated[bool, typer.Option("--test", help="Verify the channel works")] = False,
) -> None:
    """Send a Telegram message.

    Used by the scheduled workflow to report a crash, and by you to confirm the
    channel works before relying on it.
    """
    config = Config.load()
    telegram = build(config.secrets.telegram_bot_token, config.secrets.telegram_chat_id)

    if telegram is None:
        console.print(
            "[yellow]Telegram is not configured.[/yellow]\n\n"
            "  1. Message @BotFather on Telegram, send /newbot, follow the prompts\n"
            "  2. Put the token in .env as TELEGRAM_BOT_TOKEN\n"
            "  3. Send your new bot any message\n"
            "  4. Open https://api.telegram.org/bot<TOKEN>/getUpdates and copy\n"
            "     result[0].message.chat.id into .env as TELEGRAM_CHAT_ID"
        )
        raise typer.Exit(1)

    if test:
        try:
            username = telegram.check()
        except NotifyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"[green]token valid[/green] — bot is @{username}")
        message = message or "ai.rsenal is connected. This is a test message."

    text = f"⚠️ *ai.rsenal*\n\n{failure}" if failure else message
    if not text:
        console.print("[red]nothing to send.[/red] Pass a message, --failure or --test.")
        raise typer.Exit(1)

    try:
        telegram.send(text)
    except NotifyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]sent[/green]")

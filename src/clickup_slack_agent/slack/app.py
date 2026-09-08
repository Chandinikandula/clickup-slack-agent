"""Slack app wiring.

Socket Mode, so there is no public URL, no TLS certificate and no tunnel in
development — the process dials out to Slack instead of being called.
"""

import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..clickup.client import ClickUpClient
from ..config import Settings
from ..digest.service import send_digest

log = logging.getLogger(__name__)


def build_app(clickup: ClickUpClient, settings: Settings) -> App:
    app = App(token=settings.slack_bot_token, logger=log)

    @app.event("message")
    def handle_dm(event: dict, say) -> None:
        # Ignore edits, joins, and the bot's own posts, or it answers itself.
        if event.get("subtype") or event.get("bot_id"):
            return
        _respond(event.get("text", ""), event["channel"], app.client, clickup, settings, say)

    @app.event("app_mention")
    def handle_mention(event: dict, say) -> None:
        text = event.get("text", "")
        # Strip the leading "<@U123>" so the agent sees only the question.
        _, _, question = text.partition(">")
        _respond(question.strip() or text, event["channel"], app.client, clickup, settings, say)

    return app


def _respond(text: str, channel: str, slack, clickup, settings, say) -> None:
    """Route a message. The agent replaces this branch once the LLM is wired."""
    if text.strip().lower() in {"digest", "today", "brief", "standup"}:
        send_digest(clickup, slack, settings, channel=channel)
        return

    say(
        "I can show you your digest — say *digest*.\n"
        "_Conversational answers arrive once the agent layer is wired up._"
    )


def start_socket_mode(app: App, settings: Settings) -> SocketModeHandler:
    handler = SocketModeHandler(app, settings.slack_app_token)
    log.info("connecting to Slack over Socket Mode")
    handler.start()  # blocks
    return handler

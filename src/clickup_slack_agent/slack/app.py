"""Slack app wiring.

Socket Mode, so there is no public URL, no TLS certificate and no tunnel in
development — the process dials out to Slack instead of being called.
"""

import json
import logging
from collections import OrderedDict

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..agent.llm import Message, ToolCall
from ..agent.loop import Agent
from ..clickup.client import ClickUpClient
from ..config import Settings
from ..digest.service import send_digest
from .blocks import agent_blocks, confirm_write_blocks, error_blocks

log = logging.getLogger(__name__)

DIGEST_WORDS = {"digest", "today", "brief", "standup", "morning"}
# Enough for "move the first one" to resolve; short enough to stay cheap.
HISTORY_TURNS = 8
SEEN_EVENTS_MAX = 500


class Conversations:
    """Per-channel history, in memory.

    Deliberately not persisted: a restart losing your last two turns is a
    minor annoyance, and a database is not worth carrying for it yet.
    """

    def __init__(self, max_turns: int = HISTORY_TURNS) -> None:
        self._turns: dict[str, list[Message]] = {}
        self.max_turns = max_turns

    def get(self, channel: str) -> list[Message]:
        return self._turns.get(channel, [])

    def set(self, channel: str, messages: list[Message]) -> None:
        self._turns[channel] = messages[-self.max_turns :]

    def clear(self, channel: str) -> None:
        self._turns.pop(channel, None)


def build_app(clickup: ClickUpClient, settings: Settings, agent: Agent | None = None) -> App:
    app = App(token=settings.slack_bot_token, logger=log)
    conversations = Conversations()
    # Slack redelivers an event if we are slow to ack, and an agent turn can
    # take seconds. Without this you get the same question answered twice.
    seen: OrderedDict[str, None] = OrderedDict()

    def already_handled(event_id: str | None) -> bool:
        if not event_id:
            return False
        if event_id in seen:
            return True
        seen[event_id] = None
        while len(seen) > SEEN_EVENTS_MAX:
            seen.popitem(last=False)
        return False

    @app.event("message")
    def handle_dm(body: dict, event: dict, client) -> None:
        if event.get("subtype") or event.get("bot_id"):
            return
        if already_handled(body.get("event_id")):
            log.info("skipping duplicate delivery of %s", body.get("event_id"))
            return
        _respond(event.get("text", ""), event["channel"], client)

    @app.event("app_mention")
    def handle_mention(body: dict, event: dict, client) -> None:
        if already_handled(body.get("event_id")):
            return
        # Strip the leading "<@U123>" so the agent sees only the question.
        _, _, question = event.get("text", "").partition(">")
        _respond(question.strip(), event["channel"], client)

    def _respond(text: str, channel: str, client) -> None:
        question = text.strip()
        if not question:
            return

        if question.lower() in DIGEST_WORDS:
            send_digest(clickup, client, settings, channel=channel)
            return

        if question.lower() in {"reset", "forget", "clear"}:
            conversations.clear(channel)
            client.chat_postMessage(channel=channel, text="Forgotten. Fresh start.")
            return

        if agent is None:
            client.chat_postMessage(
                channel=channel,
                text=(
                    "I can show your digest — say *digest*. Conversational answers need an "
                    f"API key for `{settings.llm_provider}` in the environment."
                ),
            )
            return

        # Post first, edit later: Slack shows something immediately while the
        # agent thinks, instead of a silent gap of several seconds.
        placeholder = client.chat_postMessage(channel=channel, text="_thinking…_")
        ts = placeholder["ts"]

        try:
            result = agent.run(question, conversations.get(channel))
        except Exception as exc:  # noqa: BLE001 - the user must hear about it
            log.exception("agent failed")
            client.chat_update(
                channel=channel, ts=ts, text="Agent failed", blocks=error_blocks(str(exc))
            )
            return

        conversations.set(channel, result.messages)

        if result.pending_write:
            call = result.pending_write
            client.chat_update(
                channel=channel,
                ts=ts,
                text="Confirm this change?",
                blocks=confirm_write_blocks(
                    _describe(call, result.text),
                    json.dumps({"name": call.name, "arguments": call.arguments}),
                ),
            )
            return

        client.chat_update(
            channel=channel,
            ts=ts,
            text=result.text[:200] or "Done",
            blocks=agent_blocks(result.text, result.tools_used),
        )

    @app.action("confirm_write")
    def on_confirm(ack, body: dict, client) -> None:
        ack()
        if agent is None:
            return

        payload = json.loads(body["actions"][0]["value"])
        call = ToolCall(id="confirmed", name=payload["name"], arguments=payload["arguments"])
        log.info("confirmed write: %s %s", call.name, call.arguments)

        outcome = json.loads(agent.execute_confirmed_write(call))
        message = (
            f":x: Could not apply it — {outcome['error']}"
            if "error" in outcome
            else f":white_check_mark: Done — {_describe(call, '')}"
        )
        # Replace the buttons so the action cannot be run twice.
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=message,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}],
        )

    @app.action("cancel_write")
    def on_cancel(ack, body: dict, client) -> None:
        ack()
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Cancelled",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": ":no_entry: Cancelled."}}
            ],
        )

    return app


def _describe(call: ToolCall, preamble: str) -> str:
    args = call.arguments
    if call.name == "update_task_status":
        action = f"Move task `{args.get('task_id')}` to *{args.get('status')}*"
    elif call.name == "add_comment":
        action = f"Comment on `{args.get('task_id')}`:\n> {str(args.get('text', ''))[:300]}"
    else:
        action = f"Run `{call.name}` with `{args}`"
    return f"{preamble}\n\n{action}".strip()


def start_socket_mode(app: App, settings: Settings) -> SocketModeHandler:
    if not settings.slack_app_token:
        raise RuntimeError(
            "SLACK_APP_TOKEN is required to listen for messages. "
            "Generate one under Basic Information → App-Level Tokens."
        )
    handler = SocketModeHandler(app, settings.slack_app_token)
    log.info("connecting to Slack over Socket Mode")
    handler.start()  # blocks
    return handler

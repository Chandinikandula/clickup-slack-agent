"""Fetch, build and deliver the morning digest."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

from ..clickup.client import ClickUpClient
from ..config import Settings
from ..slack.blocks import digest_blocks, digest_text, error_blocks
from .builder import Digest, build_digest

log = logging.getLogger(__name__)


def collect_digest(
    clickup: ClickUpClient, settings: Settings, *, now: datetime | None = None
) -> Digest:
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    tasks = clickup.get_tasks(assignee_ids=[settings.clickup_user_id])
    return build_digest(tasks, tz=tz, now=now)


def send_digest(
    clickup: ClickUpClient,
    slack: WebClient,
    settings: Settings,
    *,
    channel: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Post the digest, or post the reason it could not be built.

    A digest that fails silently is worse than no digest at all — you would
    read the absence as a clear day. So failures are delivered too, and the
    return value lets a scheduled run exit non-zero and show up red.
    """
    target = channel or settings.slack_user_id
    ok = True
    try:
        digest = collect_digest(clickup, settings, now=now)
        blocks, text = digest_blocks(digest), digest_text(digest)
        log.info("digest built: %d tasks", digest.total)
    except Exception as exc:  # noqa: BLE001 - the message must go out regardless
        log.exception("digest failed")
        blocks, text = error_blocks(str(exc)), "Digest failed"
        ok = False

    slack.chat_postMessage(channel=target, blocks=blocks, text=text)
    return ok

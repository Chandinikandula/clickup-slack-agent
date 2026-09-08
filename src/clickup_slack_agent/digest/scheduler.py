"""In-process cron for the morning digest.

APScheduler rather than system cron: the app is already a long-running
process for Socket Mode, so this keeps deployment to a single container and
one place to look when something did not fire.
"""

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from slack_sdk import WebClient

from ..clickup.client import ClickUpClient
from ..config import Settings
from .service import send_digest

log = logging.getLogger(__name__)


def start_scheduler(
    clickup: ClickUpClient, slack: WebClient, settings: Settings
) -> BackgroundScheduler:
    tz = ZoneInfo(settings.timezone)
    scheduler = BackgroundScheduler(timezone=tz)

    scheduler.add_job(
        send_digest,
        trigger=CronTrigger(hour=settings.digest_hour, minute=settings.digest_minute, timezone=tz),
        args=[clickup, slack, settings],
        id="morning_digest",
        # If the process was down at 09:30, send late rather than skip — but
        # only within the hour, so a restart at 18:00 does not fire a stale one.
        misfire_grace_time=3600,
        coalesce=True,  # one send after downtime, not one per missed run
        max_instances=1,
    )

    scheduler.start()
    log.info(
        "digest scheduled for %02d:%02d %s",
        settings.digest_hour,
        settings.digest_minute,
        settings.timezone,
    )
    return scheduler

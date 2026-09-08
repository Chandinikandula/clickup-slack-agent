"""Send the morning digest once and exit.

Used two ways: by hand, to see the digest without waiting for 09:30, and by
the scheduled GitHub Action, which is why it exits non-zero on failure — a
red run is how you find out the digest stopped working.

    uv run python scripts/send_digest_now.py
"""

import logging
import sys

from slack_sdk import WebClient

from clickup_slack_agent.clickup.client import ClickUpClient
from clickup_slack_agent.config import get_settings
from clickup_slack_agent.digest.service import collect_digest, send_digest


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    settings = get_settings()

    clickup = ClickUpClient(
        settings.clickup_api_token,
        settings.clickup_team_id,
        exclude_list_ids=settings.clickup_exclude_list_ids,
        include_list_ids=settings.clickup_include_list_ids,
    )
    slack = WebClient(token=settings.slack_bot_token)

    try:
        digest = collect_digest(clickup, settings)
        print(f"\n{digest.on_date}  —  {digest.total} tasks")
        for label, bucket in (
            ("overdue", digest.overdue),
            ("due today", digest.due_today),
            ("in flight", digest.in_progress),
            ("upcoming", digest.upcoming),
        ):
            print(f"  {label:<10} {len(bucket)}")
    except Exception as exc:  # noqa: BLE001 - send_digest reports it to Slack too
        print(f"\ncould not build the digest: {exc}")

    if not send_digest(clickup, slack, settings):
        print("\nDigest failed — the error was posted to Slack.\n")
        sys.exit(1)

    print("\nSent to your Slack DM.\n")


if __name__ == "__main__":
    main()

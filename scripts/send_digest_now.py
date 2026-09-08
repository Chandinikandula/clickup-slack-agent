"""Send the morning digest immediately, without waiting for 09:30.

Run with:  uv run python scripts/send_digest_now.py
"""

from slack_sdk import WebClient

from clickup_slack_agent.clickup.client import ClickUpClient
from clickup_slack_agent.config import get_settings
from clickup_slack_agent.digest.service import collect_digest, send_digest


def main() -> None:
    settings = get_settings()
    clickup = ClickUpClient(
        settings.clickup_api_token,
        settings.clickup_team_id,
        exclude_list_ids=settings.clickup_exclude_list_ids,
        include_list_ids=settings.clickup_include_list_ids,
    )
    slack = WebClient(token=settings.slack_bot_token)

    digest = collect_digest(clickup, settings)
    print(f"\n{digest.on_date}  —  {digest.total} tasks")
    for label, bucket in (
        ("overdue", digest.overdue),
        ("due today", digest.due_today),
        ("in flight", digest.in_progress),
        ("upcoming", digest.upcoming),
    ):
        print(f"  {label:<10} {len(bucket)}")

    send_digest(clickup, slack, settings)
    print("\nSent to your Slack DM.\n")


if __name__ == "__main__":
    main()

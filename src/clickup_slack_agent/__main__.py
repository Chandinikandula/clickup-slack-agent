"""Entry point: one process running the scheduler and the Slack listener.

uv run python -m clickup_slack_agent
"""

import logging

from .clickup.client import ClickUpClient
from .config import settings
from .digest.scheduler import start_scheduler
from .slack.app import build_app, start_socket_mode


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)

    clickup = ClickUpClient(
        settings.clickup_api_token,
        settings.clickup_team_id,
        exclude_list_ids=settings.clickup_exclude_list_ids,
        include_list_ids=settings.clickup_include_list_ids,
    )
    app = build_app(clickup, settings)

    start_scheduler(clickup, app.client, settings)
    start_socket_mode(app, settings)  # blocks until interrupted


if __name__ == "__main__":
    main()

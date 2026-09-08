"""Entry point: one process running the scheduler and the Slack listener.

uv run python -m clickup_slack_agent
"""

import logging
from zoneinfo import ZoneInfo

from .agent.loop import Agent
from .agent.providers import build_provider
from .agent.tools import ToolRegistry
from .clickup.client import ClickUpClient
from .config import get_settings
from .digest.scheduler import start_scheduler
from .slack.app import build_app, start_socket_mode

log = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
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

    # The digest needs no model, so a missing key costs you the chat layer
    # rather than the whole app.
    agent = None
    if settings.agent_enabled:
        registry = ToolRegistry(clickup, settings.clickup_user_id, ZoneInfo(settings.timezone))
        agent = Agent(
            build_provider(settings.llm_provider, settings.llm_api_key, settings.llm_model),
            registry,
            timezone=settings.timezone,
        )
        log.info("agent ready: %s / %s", settings.llm_provider, settings.llm_model)
    else:
        log.warning("no API key for %s — digest only, no chat", settings.llm_provider)

    app = build_app(clickup, settings, agent)
    start_scheduler(clickup, app.client, settings)
    start_socket_mode(app, settings)  # blocks until interrupted


if __name__ == "__main__":
    main()

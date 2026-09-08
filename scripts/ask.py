"""Ask the agent a question from the terminal, and see which tools it chose.

uv run python scripts/ask.py "what is due today?"
"""

import logging
import sys
from zoneinfo import ZoneInfo

from clickup_slack_agent.agent.loop import Agent
from clickup_slack_agent.agent.providers import build_provider
from clickup_slack_agent.agent.tools import ToolRegistry
from clickup_slack_agent.clickup.client import ClickUpClient
from clickup_slack_agent.config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    settings = get_settings()
    question = " ".join(sys.argv[1:]) or "what is due today?"

    if not settings.agent_enabled:
        print(f"No API key set for provider {settings.llm_provider!r} — check .env")
        raise SystemExit(1)

    clickup = ClickUpClient(
        settings.clickup_api_token,
        settings.clickup_team_id,
        exclude_list_ids=settings.clickup_exclude_list_ids,
        include_list_ids=settings.clickup_include_list_ids,
    )
    registry = ToolRegistry(clickup, settings.clickup_user_id, ZoneInfo(settings.timezone))
    agent = Agent(
        build_provider(settings.llm_provider, settings.llm_api_key, settings.llm_model),
        registry,
        timezone=settings.timezone,
    )

    print(f"\n> {question}\n")
    result = agent.run(question)

    for i, step in enumerate(result.steps, 1):
        print(f"  [{i}] {step.tool}({step.arguments})  {step.duration_ms}ms")
        print(f"      -> {step.result[:160]}")
    if result.steps:
        print()

    if result.pending_write:
        call = result.pending_write
        print(f"  NEEDS CONFIRMATION: {call.name}({call.arguments})\n")

    print(result.text)
    print(
        f"\n  [{len(result.steps)} tool calls · {result.usage.input_tokens}in "
        f"{result.usage.output_tokens}out]\n"
    )


if __name__ == "__main__":
    main()

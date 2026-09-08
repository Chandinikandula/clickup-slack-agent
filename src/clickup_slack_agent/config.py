"""Typed configuration, loaded from the environment or a local .env file."""

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Comma-separated in .env, not JSON — NoDecode keeps pydantic-settings from
# trying to json.loads() the value before our validator sees it.
IdSet = Annotated[frozenset[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_user_id: str

    # ClickUp
    clickup_api_token: str
    clickup_team_id: str
    clickup_user_id: int
    # Lists to leave out — ClickUp's own onboarding list, say. Everything not
    # named here is included, so a new Space appears without a config change.
    clickup_exclude_list_ids: IdSet = frozenset()
    # Set this to restrict to specific lists instead; it wins over the exclude
    # list. Useful if this is ever pointed at a workspace with dozens of lists.
    clickup_include_list_ids: IdSet = frozenset()

    # LLM — only needed for the chat agent, not the digest
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"

    # Behaviour
    timezone: str = "Asia/Kolkata"
    digest_hour: int = 9
    digest_minute: int = 30

    @field_validator("clickup_exclude_list_ids", "clickup_include_list_ids", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return frozenset(part.strip() for part in v.split(",") if part.strip())
        return v


settings = Settings()  # raises at import time if a required key is missing

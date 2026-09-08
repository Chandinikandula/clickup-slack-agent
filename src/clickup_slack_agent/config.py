"""Typed configuration, loaded from the environment or a local .env file."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Comma-separated in .env, not JSON — NoDecode keeps pydantic-settings from
# trying to json.loads() the value before our validator sees it.
IdSet = Annotated[frozenset[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Slack
    slack_bot_token: str
    slack_user_id: str
    # Only Socket Mode needs this, so a one-shot digest run — the scheduled
    # GitHub Action, say — can work without it.
    slack_app_token: str = ""

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

    # LLM — only needed for the chat agent, not the digest.
    # The loop is provider-neutral; this picks which one it talks to.
    llm_provider: Literal["gemini", "anthropic", "groq"] = "gemini"
    llm_model: str = "gemini-3.5-flash"
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    @property
    def llm_api_key(self) -> str:
        return {
            "gemini": self.gemini_api_key,
            "anthropic": self.anthropic_api_key,
            "groq": self.groq_api_key,
        }[self.llm_provider]

    @property
    def agent_enabled(self) -> bool:
        """False until a key for the selected provider is present.

        The digest works without any LLM, so a missing key degrades the chat
        layer rather than stopping the app from starting.
        """
        return bool(self.llm_api_key)

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings, raising if a required key is missing.

    Deliberately a function rather than a module-level instance: importing a
    module should not require a populated environment, or CI has to carry
    real credentials just to import the code it is testing.
    """
    return Settings()

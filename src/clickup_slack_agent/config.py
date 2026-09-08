"""Typed configuration, loaded from the environment or a local .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_user_id: str

    # ClickUp
    clickup_api_token: str
    clickup_team_id: str
    clickup_user_id: str

    # LLM
    anthropic_api_key: str
    llm_model: str = "claude-sonnet-5"

    # Behaviour
    timezone: str = "Asia/Kolkata"
    digest_hour: int = 9
    digest_minute: int = 30


settings = Settings()  # raises at import time if a required key is missing

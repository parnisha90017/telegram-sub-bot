from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    bot_username: str

    crypto_pay_token: str
    crypto_pay_network: Literal["main", "test"] = "main"
    crypto_pay_webhook_url: str = ""

    telegram_webhook_url: str = ""
    telegram_webhook_secret: str

    chat_ids: list[int] = Field(default_factory=list)

    database_url: str
    port: int = 8080
    log_level: str = "INFO"

    @field_validator("chat_ids", mode="before")
    @classmethod
    def _parse_chat_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("chat_ids")
    @classmethod
    def _check_four_chats(cls, v: list[int]) -> list[int]:
        if len(v) != 4:
            raise ValueError(f"CHAT_IDS must contain exactly 4 IDs, got {len(v)}")
        return v


settings = Settings()  # type: ignore[call-arg]

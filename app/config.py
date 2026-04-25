from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


# pydantic-settings ≤ 2.6 JSON-decodes "complex" fields (list/dict/etc.) from
# env / .env BEFORE field_validator(mode="before") runs, which makes a CSV
# value like `cryptobot,heleket` crash with JSONDecodeError. NoDecode /
# enable_decoding would solve this in newer versions, but pinning is risky for
# prod, so we short-circuit the parse for known CSV fields by subclassing
# both env sources (port from sid-bot/src/config.py).
# Только enabled_providers идёт через CSV-source. chat_ids в проде хранится
# как JSON-список (`["-100...","-100..."]`) — для него хватает встроенного
# JSON-decode pydantic-settings + field_validator(mode="before") ниже.
CSV_LIST_FIELDS = {"enabled_providers"}


def _maybe_split_csv(field_name: str, value: Any) -> Any:
    if field_name in CSV_LIST_FIELDS and isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return value


class _CSVEnvSource(EnvSettingsSource):
    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        value = _maybe_split_csv(field_name, value)
        if isinstance(value, list):
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class _CSVDotEnvSource(DotEnvSettingsSource):
    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        value = _maybe_split_csv(field_name, value)
        if isinstance(value, list):
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    bot_username: str

    # CryptoBot
    crypto_pay_token: str
    crypto_pay_network: Literal["main", "test"] = "main"
    crypto_pay_webhook_url: str = ""

    # Heleket (TRC-20 USDT)
    heleket_merchant_uuid: str = Field(default="", alias="HELEKET_MERCHANT_UUID")
    heleket_api_key: str = Field(default="", alias="HELEKET_API_KEY")
    heleket_webhook_path: str = Field(
        default="/heleket/webhook", alias="HELEKET_WEBHOOK_PATH"
    )
    heleket_webhook_url: str = Field(default="", alias="HELEKET_WEBHOOK_URL")

    # Какие провайдеры активны (CSV в env)
    enabled_providers: list[str] = Field(
        default_factory=lambda: ["cryptobot"], alias="ENABLED_PROVIDERS"
    )

    telegram_webhook_url: str = ""
    telegram_webhook_secret: str

    chat_ids: list[int] = Field(default_factory=list)

    database_url: str
    port: int = 8080
    log_level: str = "INFO"

    # Один Telegram-ID администратора. Команды /find /export /stats /extend
    # /revoke /cleanup_chats /pending /health доступны только этому ID.
    # 0 (default) → нет администратора, все команды silent no-op.
    admin_telegram_id: int = Field(default=0, alias="ADMIN_TELEGRAM_ID")

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

    @field_validator("enabled_providers", mode="before")
    @classmethod
    def _parse_providers_csv(cls, v: object) -> object:
        # belt-and-suspenders to the CSV source above
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Swap the source classes in place so prepare_field_value picks up
        # the CSV short-circuit; priority order is unchanged.
        if isinstance(env_settings, EnvSettingsSource):
            env_settings.__class__ = _CSVEnvSource
        if isinstance(dotenv_settings, DotEnvSettingsSource):
            dotenv_settings.__class__ = _CSVDotEnvSource
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)


settings = Settings()  # type: ignore[call-arg]

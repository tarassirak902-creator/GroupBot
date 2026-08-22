from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    database_url: str
    log_level: str = "INFO"
    creator_ids: tuple[int, ...] = ()

    @field_validator("creator_ids", mode="before")
    @classmethod
    def parse_creator_ids(cls, value):
        if value in (None, ""):
            return ()
        if isinstance(value, str):
            return tuple(int(item.strip()) for item in value.split(",") if item.strip())
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    database_url: str
    log_level: str = "INFO"
    creator_ids: str = ""

    @property
    def creator_id_set(self) -> set[int]:
        return {int(item.strip()) for item in self.creator_ids.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

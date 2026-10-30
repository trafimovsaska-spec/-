from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    BOT_TOKEN: str
    API_ID: int
    API_HASH: str

    DATA_DIR: str = "/data"
    ALLOWED_USER_IDS: str = ""

    BATCH_LIMIT: int = 25          # максимум номеров за одну массовую проверку
    CHECK_DELAY_MIN: float = 3.0   # пауза между номерами (анти-флуд)
    CHECK_DELAY_MAX: float = 7.0

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.ALLOWED_USER_IDS.split(",") if x.strip().isdigit()}


settings = Settings()

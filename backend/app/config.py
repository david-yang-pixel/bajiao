from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    BINANCE_API_KEY: str = ""
    BINANCE_MCP_URL: str = ""
    FEISHU_WEBHOOK_URL: str = ""
    OPENAI_API_KEY: str = ""
    DB_PATH: str = "sqlite.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

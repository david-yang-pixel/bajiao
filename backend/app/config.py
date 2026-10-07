from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CHECK_INTERVAL_HOURS = 4.0
DEFAULT_INCLUDE_AI_ANALYSIS = True

class Settings(BaseSettings):
    BINANCE_API_KEY: str = ""
    BINANCE_API_SECRET: str = ""
    BINANCE_MCP_URL: str = "https://agent.binance.com/mcp/agentic"
    FEISHU_WEBHOOK_URL: str = ""
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    WECOM_WEBHOOK_URL: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    DB_PATH: str = "sqlite.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

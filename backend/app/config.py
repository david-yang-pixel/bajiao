from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    BINANCE_API_KEY: str = ""
    BINANCE_API_SECRET: str = ""
    BINANCE_MCP_URL: str = "https://agent.binance.com/mcp/agentic"
    FEISHU_WEBHOOK_URL: str = ""
    WECOM_WEBHOOK_URL: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    DB_PATH: str = "sqlite.db"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

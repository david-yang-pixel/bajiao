import re
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from typing import List, Optional, Literal
from typing import Annotated

LadderPercent = Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]

class PositionCreate(BaseModel):
    symbol: str = Field(pattern=r'^(?:[A-Za-z][A-Za-z0-9]{0,24}|EQ_[A-Z][A-Z0-9.\-]{0,19})$')
    buy_price: float = Field(gt=0, allow_inf_nan=False)
    cost_currency: Optional[Literal['USD', 'USDT', 'USDC']] = None
    quantity: float = Field(gt=0, allow_inf_nan=False)
    take_profit_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    stop_loss_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    check_interval_hours: Optional[float] = Field(default=None, ge=0, le=168, allow_inf_nan=False)
    ladder_alerts: List[LadderPercent] = Field(default_factory=list)
    include_ai_analysis: Optional[bool] = None
    
    model_config = ConfigDict(from_attributes=True)

class PositionOut(PositionCreate):
    id: int
    is_active: bool

class PositionUpdate(BaseModel):
    buy_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    cost_currency: Optional[Literal['USD', 'USDT', 'USDC']] = None
    quantity: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    take_profit_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    stop_loss_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    ladder_alerts: Optional[List[LadderPercent]] = None
    check_interval_hours: Optional[float] = Field(default=None, ge=0, le=168, allow_inf_nan=False)
    include_ai_analysis: Optional[bool] = None

class AskAIRequest(BaseModel):
    question: str
    symbol: Optional[str] = None

class RecommendationCreate(BaseModel):
    symbol: str
    reason: str
    target_price: Optional[float] = None
    
class RecommendationOut(RecommendationCreate):
    id: int
    created_at: str
    
class AlertLogCreate(BaseModel):
    position_id: Optional[int] = None
    symbol: str
    message: str
    alert_type: str
    
class AlertLogOut(AlertLogCreate):
    id: int
    created_at: str

class SystemConfigSchema(BaseModel):
    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None
    binance_mcp_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: Optional[str] = None
    feishu_webhook_url: Optional[str] = None
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    wecom_webhook_url: Optional[str] = None
    ai_picks_push_enabled: Optional[bool] = None
    ai_picks_push_time: Optional[str] = Field(default=None, pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    default_check_interval_hours: Optional[float] = Field(default=None, ge=0, le=168, allow_inf_nan=False)
    default_include_ai_analysis: Optional[bool] = None
    fmp_api_key: Optional[SecretStr] = None
    analysis_top_n: Optional[int] = Field(default=None, ge=3, le=5)
    analysis_min_score: Optional[int] = Field(default=None, ge=40, le=100)
    earnings_blackout_days: Optional[int] = Field(default=None, ge=0, le=14)
    ai_picks_push_timezone: Optional[str] = None

    @field_validator('ai_picks_push_timezone')
    @classmethod
    def valid_timezone(cls, value):
        if value is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('请选择有效的 IANA 时区') from None
        return value

class TestConnectionRequest(BaseModel):
    service: str # binance | llm | feishu | wecom | feishu_bot
    api_key: Optional[str] = None
    api_secret: Optional[SecretStr] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    webhook_url: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None


class BinanceCredentialsUpdate(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr
    current_api_secret: Optional[SecretStr] = None

    @field_validator("api_key", "api_secret")
    @classmethod
    def validate_hmac_credential(cls, value: SecretStr) -> SecretStr:
        cleaned = value.get_secret_value().strip()
        if not re.fullmatch(r"[A-Za-z0-9]{64}", cleaned):
            raise ValueError("请填写完整的 64 位币安 HMAC API Key 和 API Secret")
        return SecretStr(cleaned)

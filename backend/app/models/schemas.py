from pydantic import BaseModel, ConfigDict
from typing import List, Optional

class PositionCreate(BaseModel):
    symbol: str
    buy_price: float
    quantity: float
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    check_interval_hours: float = 0.5
    ladder_alerts: List[float] = []
    include_ai_analysis: bool = False
    
    model_config = ConfigDict(from_attributes=True)

class PositionOut(PositionCreate):
    id: int
    is_active: bool

class PositionUpdate(BaseModel):
    buy_price: Optional[float] = None
    quantity: Optional[float] = None
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    ladder_alerts: Optional[List[float]] = None
    check_interval_hours: Optional[float] = None
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
    ai_picks_push_time: Optional[str] = None

class TestConnectionRequest(BaseModel):
    service: str # binance | llm | feishu | wecom | feishu_bot
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    webhook_url: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None

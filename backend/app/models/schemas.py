from pydantic import BaseModel, ConfigDict
from typing import List, Optional

class PositionCreate(BaseModel):
    symbol: str
    buy_price: float
    quantity: float
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    check_interval_hours: int = 1
    ladder_alerts: List[float] = []
    
    model_config = ConfigDict(from_attributes=True)

class PositionOut(PositionCreate):
    id: int
    is_active: bool

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

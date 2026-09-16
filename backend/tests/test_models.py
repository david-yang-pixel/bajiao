import pytest
from backend.app.models.schemas import PositionCreate, PositionOut

def test_position_create_schema_valid():
    data = {
        "symbol": "NVDABUSDT",
        "buy_price": 135.5,
        "quantity": 10.0,
        "take_profit_price": 150.0,
        "stop_loss_price": 125.0,
        "check_interval_hours": 2,
        "ladder_alerts": [0.05, 0.10]
    }
    pos = PositionCreate(**data)
    assert pos.symbol == "NVDABUSDT"
    assert pos.buy_price == 135.5
    assert pos.check_interval_hours == 2

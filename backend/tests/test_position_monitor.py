import pytest
from unittest.mock import AsyncMock
from backend.app.database import init_db
from backend.app.services.position_monitor import PositionMonitor

@pytest.mark.asyncio
async def test_position_monitor_alerts(tmp_path):
    db_file = str(tmp_path / "test_monitor.db")
    await init_db(db_file)

    mock_client = AsyncMock()
    mock_notify = AsyncMock()
    mock_notify.send_position_alert.return_value = True

    monitor = PositionMonitor(
        binance_client=mock_client,
        notification_service=mock_notify,
        db_path=db_file
    )

    pos = {
        "id": 1,
        "symbol": "NVDABUSDT",
        "buy_price": 100.0,
        "quantity": 10.0,
        "take_profit_price": 120.0,
        "stop_loss_price": 90.0,
        "check_interval_hours": 1,
        "ladder_alerts": [5.0, 10.0],
        "is_active": 1
    }

    # Scenario 1: Price goes to 125.0 -> breaches take profit & ladder alerts
    mock_client.get_ticker_price.return_value = 125.0
    triggered = await monitor.check_single_position(pos)

    assert "take_profit" in triggered
    assert "ladder_5.0" in triggered
    assert "ladder_10.0" in triggered
    assert mock_notify.send_position_alert.call_count == 3

    # Scenario 2: Anti-spam cooldown: immediate second check should trigger 0 alerts
    mock_notify.send_position_alert.reset_mock()
    triggered_again = await monitor.check_single_position(pos)
    assert len(triggered_again) == 0
    assert mock_notify.send_position_alert.call_count == 0

    # Scenario 3: Periodic alert forced
    mock_notify.send_position_alert.reset_mock()
    periodic_triggered = await monitor.check_single_position(pos, force_periodic=True)
    assert "periodic" in periodic_triggered
    assert mock_notify.send_position_alert.call_count == 1

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

@pytest.mark.asyncio
async def test_periodic_inspection_interval(tmp_path):
    import time
    db_file = str(tmp_path / "test_periodic.db")
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
        "id": 2,
        "symbol": "AMZNBUSDT",
        "buy_price": 200.0,
        "quantity": 1.0,
        "check_interval_hours": 4.0,  # 4 hours
        "is_active": 1
    }
    mock_client.get_ticker_price.return_value = 200.0

    # 1. First check: should initialize timer and NOT send periodic alert
    triggered = await monitor.check_single_position(pos)
    assert "periodic" not in triggered
    assert mock_notify.send_position_alert.call_count == 0

    # 2. Check after 1 hour (3600s elapsed < 4 * 3600s): should NOT trigger
    monitor._last_periodic_times[2] = time.time() - 3600
    triggered = await monitor.check_single_position(pos)
    assert "periodic" not in triggered
    assert mock_notify.send_position_alert.call_count == 0

    # 3. Check after 4 hours (14400s elapsed >= 4 * 3600s): should trigger
    monitor._last_periodic_times[2] = time.time() - 14405
    triggered = await monitor.check_single_position(pos)
    assert "periodic" in triggered
    assert mock_notify.send_position_alert.call_count == 1

    # 4. Immediate subsequent check: should NOT trigger again
    mock_notify.send_position_alert.reset_mock()
    triggered = await monitor.check_single_position(pos)
    assert "periodic" not in triggered
    assert mock_notify.send_position_alert.call_count == 0

    # 5. Recovery from DB alert_logs on new monitor instance (e.g. server restart)
    new_monitor = PositionMonitor(
        binance_client=mock_client,
        notification_service=mock_notify,
        db_path=db_file
    )
    # The alert was logged in step 3, so DB has it as recently sent (< 4 hours)
    # check_single_position should load last alert time from DB and NOT fire
    triggered = await new_monitor.check_single_position(pos)
    assert "periodic" not in triggered
    assert mock_notify.send_position_alert.call_count == 0

@pytest.mark.asyncio
async def test_ladder_milestone_no_repeat(tmp_path):
    db_file = str(tmp_path / "test_ladder.db")
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
        "id": 2,
        "symbol": "AMZNBUSDT",
        "buy_price": 200.0,
        "quantity": 1.0,
        "check_interval_hours": 4.0,
        "ladder_alerts": [2.0, 3.0, 5.0],
        "is_active": 1
    }

    # Step 1: Price goes to 204.5 (+2.25%) -> breaches 2.0% step -> should alert ONCE
    mock_client.get_ticker_price.return_value = 204.5
    triggered = await monitor.check_single_position(pos)
    assert "ladder_2.0" in triggered
    assert mock_notify.send_position_alert.call_count == 1

    # Step 2: Second check (e.g. 30 mins later with cooldown reset) -> should NOT alert
    mock_notify.send_position_alert.reset_mock()
    monitor._last_alert_times.clear()  # Even if old cooldown was bypassed
    triggered = await monitor.check_single_position(pos)
    assert "ladder_2.0" not in triggered
    assert mock_notify.send_position_alert.call_count == 0

    # Step 3: Server restart (new instance, empty in-memory state) -> recovers from DB alert_logs -> NO alert
    new_monitor = PositionMonitor(
        binance_client=mock_client,
        notification_service=mock_notify,
        db_path=db_file
    )
    triggered = await new_monitor.check_single_position(pos)
    assert "ladder_2.0" not in triggered
    assert mock_notify.send_position_alert.call_count == 0

    # Step 4: Price rises to 206.5 (+3.25%) -> reaches next milestone 3.0% -> should alert 3.0% ONCE
    mock_client.get_ticker_price.return_value = 206.5
    triggered = await new_monitor.check_single_position(pos)
    assert "ladder_3.0" in triggered
    assert "ladder_2.0" not in triggered
    assert mock_notify.send_position_alert.call_count == 1



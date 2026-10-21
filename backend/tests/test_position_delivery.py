"""Delivery regressions: real SQLite state, mocked exchange/notification/AI."""
import asyncio
import time
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from backend.app.services.position_monitor import PositionMonitor
from backend.app.database import init_db


async def make_monitor(tmp_path, **overrides):
    path = str(tmp_path / "delivery.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY, symbol TEXT, buy_price REAL,
                quantity REAL, take_profit_price REAL, stop_loss_price REAL,
                check_interval_hours REAL, ladder_alerts TEXT,
                include_ai_analysis INTEGER, is_active INTEGER
            );
            CREATE TABLE alert_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER,
                symbol TEXT, message TEXT, alert_type TEXT,
                status TEXT NOT NULL DEFAULT 'success',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO positions VALUES
                (1, 'NVDABUSDT', 100, 2, 110, NULL, 0, '[]', 1, 1);
        """)
        await db.commit()
    await init_db(path)
    client = AsyncMock()
    client.get_ticker_price.return_value = 115.0
    notify = AsyncMock()
    notify.send_position_alert.return_value = True
    notify.send_text.return_value = True
    monitor = PositionMonitor(binance_client=client, notification_service=notify, db_path=path)
    pos = dict(id=1, symbol="NVDABUSDT", buy_price=100, quantity=2,
               take_profit_price=110, check_interval_hours=0, ladder_alerts=[],
               include_ai_analysis=False, is_active=1)
    pos.update(overrides)
    return monitor, pos, notify


async def statuses(monitor):
    async with aiosqlite.connect(monitor.db_path) as db:
        rows = await (await db.execute("SELECT status FROM alert_logs ORDER BY id")).fetchall()
    return [row[0] for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_type", ["take_profit", "stop_loss", "ladder", "periodic"])
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_delivery_retries_without_advancing_state(tmp_path, caplog, alert_type, raises):
    monitor, pos, notify = await make_monitor(tmp_path, take_profit_price=None)
    if alert_type == "take_profit":
        pos["take_profit_price"] = 110
    elif alert_type == "stop_loss":
        pos["stop_loss_price"] = 120
    elif alert_type == "ladder":
        pos["ladder_alerts"] = [10]
    else:
        pos["check_interval_hours"] = 1
        monitor._last_periodic_times[1] = time.time() - 7200
    old_periodic = monitor._last_periodic_times.get(1)
    notify.send_position_alert.side_effect = [RuntimeError("secret-remote-url") if raises else False, True]
    assert await monitor.check_single_position(pos) == []
    assert monitor._last_periodic_times.get(1) == old_periodic
    assert not monitor._achieved_milestones
    assert await statuses(monitor) == ["failed"]
    assert "secret-remote-url" not in caplog.text
    expected = "ladder_10.0" if alert_type == "ladder" else alert_type
    assert await monitor.check_single_position(pos) == [expected]
    assert await statuses(monitor) == ["failed", "success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_type", ["take_profit", "stop_loss", "ladder", "periodic"])
async def test_risk_alert_precedes_blocked_ai(tmp_path, alert_type):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True, take_profit_price=None)
    if alert_type == "take_profit":
        pos["take_profit_price"] = 110
    elif alert_type == "stop_loss":
        pos["stop_loss_price"] = 120
    elif alert_type == "ladder":
        pos["ladder_alerts"] = [10]
    started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def analyze(symbol):
        assert notify.send_position_alert.await_count == 1
        started.set()
        await release.wait()
        return {"reason": "trend reason", "target_price": 130, "stop_loss_price": 95,
                "price_basis": "Binance token price"}

    async def send_text(content):
        sent.set()
        return True

    monitor.get_symbol_trend_analysis = AsyncMock(side_effect=analyze)
    notify.send_text.side_effect = send_text
    try:
        result = await asyncio.wait_for(monitor.check_single_position(pos, force_periodic=alert_type == "periodic"), .5)
        assert result == ["ladder_10.0" if alert_type == "ladder" else alert_type]
        await asyncio.wait_for(started.wait(), .5)
        notify.send_text.assert_not_awaited()
        assert notify.send_position_alert.call_args.kwargs.get("trend_analysis") is None
        release.set()
        await asyncio.wait_for(sent.wait(), .5)
        content = notify.send_text.call_args.args[0]
        for label in ("NVDABUSDT", "2–4", "trend reason", "Binance", "130", "95"):
            assert label in content
    finally:
        release.set()
        if hasattr(monitor, "close"):
            await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("force_periodic", [False, True])
async def test_concurrent_checks_deduplicate_delivery(tmp_path, force_periodic):
    monitor, pos, notify = await make_monitor(tmp_path)
    if force_periodic:
        pos["take_profit_price"] = None

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(.02)
        return True

    notify.send_position_alert.side_effect = slow_send
    results = await asyncio.gather(*(monitor.check_single_position(pos, force_periodic=force_periodic) for _ in range(5)))
    assert sum(len(result) for result in results) == 1
    assert notify.send_position_alert.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_type", ["take_profit", "stop_loss", "ladder", "periodic"])
async def test_failed_history_does_not_suppress_restart_alert(tmp_path, alert_type):
    monitor, pos, notify = await make_monitor(tmp_path, take_profit_price=None)
    if alert_type == "take_profit":
        pos["take_profit_price"] = 110
    elif alert_type == "stop_loss":
        pos["stop_loss_price"] = 120
    elif alert_type == "ladder":
        pos["ladder_alerts"] = [10]
    else:
        pos["check_interval_hours"] = 1
    async with aiosqlite.connect(monitor.db_path) as db:
        if alert_type == "periodic":
            await db.execute("INSERT INTO alert_logs(position_id,symbol,message,alert_type,status,created_at) VALUES(1,?,'old','periodic','success','2000-01-01 00:00:00')", (pos["symbol"],))
        await db.execute("INSERT INTO alert_logs(position_id,symbol,message,alert_type,status) VALUES(1,?,?,?,'failed')",
                         (pos["symbol"], "收益率达成阶梯目标 +10.0%", alert_type))
        await db.commit()
    expected = "ladder_10.0" if alert_type == "ladder" else alert_type
    assert await monitor.check_single_position(pos) == [expected]
    notify.send_position_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_cancels_and_joins_supplement(tmp_path):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def analyze(symbol):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monitor.get_symbol_trend_analysis = AsyncMock(side_effect=analyze)
    try:
        await asyncio.wait_for(monitor.check_single_position(pos), .5)
        await asyncio.wait_for(started.wait(), .5)
        await asyncio.wait_for(monitor.close(), .5)
        assert cancelled.is_set()
        notify.send_text.assert_not_awaited()
        await monitor.close()
    finally:
        if hasattr(monitor, "close"):
            await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["DELETE FROM positions WHERE id=1", "UPDATE positions SET is_active=0 WHERE id=1"])
async def test_supplement_skips_deleted_or_inactive_position(tmp_path, mutation):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def analyze(symbol):
        started.set()
        await release.wait()
        finished.set()
        return {"reason": "reason"}

    monitor.get_symbol_trend_analysis = AsyncMock(side_effect=analyze)
    try:
        await asyncio.wait_for(monitor.check_single_position(pos), .5)
        await asyncio.wait_for(started.wait(), .5)
        async with aiosqlite.connect(monitor.db_path) as db:
            await db.execute(mutation)
            await db.commit()
        release.set()
        await asyncio.wait_for(finished.wait(), .5)
        # Join without cancelling so the active-position guard actually runs.
        await asyncio.gather(*monitor._supplement_tasks.values())
        notify.send_text.assert_not_awaited()
    finally:
        release.set()
        if hasattr(monitor, "close"):
            await monitor.close()


@pytest.mark.asyncio
async def test_only_one_concurrent_supplement_per_position(tmp_path):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def analyze(symbol):
        started.set()
        await release.wait()
        return {"reason": "reason"}

    monitor.get_symbol_trend_analysis = AsyncMock(side_effect=analyze)
    try:
        await asyncio.wait_for(monitor.check_single_position(pos), .5)
        await asyncio.wait_for(started.wait(), .5)
        assert await monitor.check_single_position(pos, force_periodic=True) == ["periodic"]
        monitor.get_symbol_trend_analysis.assert_awaited_once()
        assert notify.send_position_alert.await_count == 2
    finally:
        release.set()
        if hasattr(monitor, "close"):
            await monitor.close()


@pytest.mark.asyncio
async def test_supplement_failure_is_isolated_and_redacted(tmp_path, caplog):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    monitor.get_symbol_trend_analysis = AsyncMock(side_effect=RuntimeError("secret-remote-url"))
    try:
        assert await asyncio.wait_for(monitor.check_single_position(pos), .5) == ["take_profit"]
        await asyncio.gather(*monitor._supplement_tasks.values())
        assert "secret-remote-url" not in caplog.text
        assert await monitor.check_single_position(pos, force_periodic=True) == ["periodic"]
    finally:
        if hasattr(monitor, "close"):
            await monitor.close()


@pytest.mark.asyncio
async def test_scan_continues_after_notification_exception(tmp_path):
    monitor, pos, notify = await make_monitor(tmp_path)
    other = dict(pos, id=2, symbol="AMZNBUSDT")
    monitor.get_active_positions = AsyncMock(return_value=[pos, other])
    notify.send_position_alert.side_effect = [RuntimeError("secret-remote-url"), True]
    await monitor.scan_all_positions()
    assert notify.send_position_alert.await_count == 2
    assert await statuses(monitor) == ["failed", "success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["buy_price", "current_price"])
@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "invalid", None])
async def test_invalid_prices_skip_safely(tmp_path, field, bad):
    monitor, pos, notify = await make_monitor(tmp_path)
    if field == "buy_price":
        pos[field] = bad
    else:
        monitor.binance_client.get_ticker_price.return_value = bad
    assert await monitor.check_single_position(pos, force_periodic=True) == []
    notify.send_position_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_trend_provider_overrides_legacy_fallback(tmp_path):
    monitor, _, _ = await make_monitor(tmp_path)
    analysis = {"reason": "2–4 week swing", "target_price": 120}
    monitor.trend_provider = AsyncMock(return_value=analysis)
    assert await monitor.get_symbol_trend_analysis("NVDABUSDT") == analysis
    monitor.trend_provider.assert_awaited_once_with("NVDABUSDT")
    monitor.binance_client.get_klines.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_risk_delivery_does_not_start_ai(tmp_path):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    monitor.get_symbol_trend_analysis = AsyncMock()
    notify.send_position_alert.return_value = False
    try:
        assert await monitor.check_single_position(pos) == []
        await asyncio.sleep(0)
        monitor.get_symbol_trend_analysis.assert_not_awaited()
        assert not monitor._supplement_tasks
    finally:
        await monitor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_supplement_send_failure_does_not_change_risk_state(tmp_path, caplog, raises):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    monitor.get_symbol_trend_analysis = AsyncMock(return_value={"reason": "reason"})
    if raises:
        notify.send_text.side_effect = RuntimeError("secret-remote-url")
    else:
        notify.send_text.return_value = False
    try:
        assert await monitor.check_single_position(pos) == ["take_profit"]
        await asyncio.gather(*monitor._supplement_tasks.values())
        assert await statuses(monitor) == ["success"]
        assert await monitor.check_single_position(pos) == []
        assert "secret-remote-url" not in caplog.text
        notify.send_text.assert_awaited_once()
        assert not monitor._supplement_tasks
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_closed_monitor_still_sends_risk_without_new_supplements(tmp_path):
    monitor, pos, notify = await make_monitor(tmp_path, include_ai_analysis=True)
    monitor.get_symbol_trend_analysis = AsyncMock()
    await monitor.close()
    assert await monitor.check_single_position(pos) == ["take_profit"]
    assert not monitor._supplement_tasks
    monitor.get_symbol_trend_analysis.assert_not_awaited()
    notify.send_position_alert.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_type", ["take_profit", "stop_loss", "ladder", "periodic"])
async def test_successful_history_survives_restart(tmp_path, alert_type):
    monitor, pos, notify = await make_monitor(tmp_path, take_profit_price=None)
    if alert_type == "take_profit":
        pos["take_profit_price"] = 110
    elif alert_type == "stop_loss":
        pos["stop_loss_price"] = 120
    elif alert_type == "ladder":
        pos["ladder_alerts"] = [2.0, 20.0]
    else:
        pos["check_interval_hours"] = 1
    first = await monitor.check_single_position(pos, force_periodic=alert_type == "periodic")
    assert first == ["ladder_2.0" if alert_type == "ladder" else alert_type]
    restarted = PositionMonitor(binance_client=monitor.binance_client,
                                notification_service=notify, db_path=monitor.db_path)
    assert await restarted.check_single_position(pos) == []
    notify.send_position_alert.assert_awaited_once()
    assert await statuses(monitor) == ["success"]

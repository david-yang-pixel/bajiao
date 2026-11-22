import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

from backend.app import main
from backend.app.database import init_db
from backend.app.services.binance_client import BinanceClient


async def prepare_db(tmp_path, monkeypatch):
    path = str(tmp_path / "preferences.db")
    monkeypatch.setattr(main.settings, "DB_PATH", path)
    await init_db(path)
    return path


def mock_holdings(monkeypatch):
    monkeypatch.setattr(main.binance_client, "api_key", "test-key")
    monkeypatch.setattr(main.binance_client, "api_secret", "test-secret")
    monkeypatch.setattr(main.binance_client, "get_account_balances", AsyncMock(return_value=[
        {"asset": "TSLAB", "total": 2.0, "wallet": "现货账户"}
    ]))
    monkeypatch.setattr(main.binance_client, "get_ticker_price", AsyncMock(return_value=100.0))
    monkeypatch.setattr(main.binance_client, "get_average_buy_price", AsyncMock(return_value=90.0))


async def short_term_runner(tmp_path, monkeypatch):
    """Real ShortTermService over fake data/agent boundaries, wired into main."""
    from backend.app.services.analysis_store import AnalysisStore
    from backend.app.services.short_term import service as svc_mod
    from backend.app.services.short_term.service import ShortTermService
    from backend.tests.test_short_term_service import FakeAgent, FakeAlpaca, FakeBinance, FakeUniverse, PRICE, selected
    from backend.tests.test_short_term_tracking import FakeNotifier

    async def quote(client, pair, price):
        return {'symbol': pair.symbol, 'mid_usdt': PRICE, 'tradable': True, 'status': 'TRADING'}
    monkeypatch.setattr(svc_mod, 'get_token_quote', quote)
    path = await prepare_db(tmp_path, monkeypatch)
    notifier = FakeNotifier()
    runner = ShortTermService(FakeBinance(), FakeAlpaca(), FakeAgent({'NVDA': selected('NVDA'), 'TSLA': selected('TSLA')}),
                              notifier, AnalysisStore())
    runner.universe = FakeUniverse()
    monkeypatch.setattr(main, 'short_term', runner)
    return runner, notifier, path


@pytest.mark.asyncio
@pytest.mark.parametrize("push", [False, True])
async def test_manual_analysis_only_sends_when_requested(tmp_path, monkeypatch, push):
    runner, notifier, path = await short_term_runner(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        response = await client.post('/api/analysis/run' + ('?push_notifications=true' if push else ''))
        assert response.status_code == 200
        assert response.json()['count'] == 2
        assert len((await client.get('/api/analysis/picks')).json()) == 2
    assert bool(notifier.sent) is push


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [4.0, 0.0, 8.0])
async def test_new_positions_use_saved_default(tmp_path, monkeypatch, interval):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    monkeypatch.setattr(main, "apply_runtime_config", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        if interval != 4:
            saved = await client.post("/api/config", json={"default_check_interval_hours": interval})
            assert saved.status_code == 200
        config = (await client.get("/api/config")).json()
        assert config["default_check_interval_hours"] == interval
        created = await client.post("/api/positions", json={
            "symbol": "NVDABUSDT", "buy_price": 100.0, "quantity": 1.0
        })
        assert created.status_code == 200
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT check_interval_hours FROM positions ORDER BY id")
        assert [row[0] for row in await cursor.fetchall()] == [interval, interval]


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [0.0, 4.0, 8.0])
async def test_sync_retains_settings_when_position_is_reimported(tmp_path, monkeypatch, interval):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            INSERT INTO positions (symbol, buy_price, quantity, check_interval_hours,
                                   ladder_alerts, include_ai_analysis, preferences_customized, is_active)
            VALUES ('TSLABUSDT', 90, 1, ?, '[5.0, 10.0]', 1, 1, 0)
        """, (interval,))
        await db.commit()
    result = await main.perform_binance_sync()
    assert result["count"] == 1
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("""
            SELECT check_interval_hours, ladder_alerts, include_ai_analysis, quantity
            FROM positions WHERE is_active = 1
        """)
        assert await cursor.fetchall() == [(interval, '[5.0, 10.0]', 1, 2.0)]


@pytest.mark.asyncio
async def test_reimport_ignores_legacy_uncustomized_settings(tmp_path, monkeypatch):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    async with aiosqlite.connect(path) as db:
        # Row left by the old sync code: hardcoded 30m interval, AI off, never edited.
        await db.execute("""
            INSERT INTO positions (symbol, buy_price, quantity, check_interval_hours,
                                   ladder_alerts, include_ai_analysis, is_active)
            VALUES ('TSLABUSDT', 90, 1, 0.5, '[2.0, 3.0, 5.0, 8.0]', 0, 0)
        """)
        await db.commit()
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("""
            SELECT check_interval_hours, include_ai_analysis FROM positions WHERE is_active = 1
        """)
        assert await cursor.fetchall() == [(4.0, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_ai", [True, False])
async def test_new_positions_use_saved_ai_default(tmp_path, monkeypatch, include_ai):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    monkeypatch.setattr(main, "apply_runtime_config", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/config")).json()["default_include_ai_analysis"] is True
        saved = await client.post("/api/config", json={"default_include_ai_analysis": include_ai})
        assert saved.status_code == 200
        assert (await client.get("/api/config")).json()["default_include_ai_analysis"] is include_ai
        created = await client.post("/api/positions", json={
            "symbol": "NVDABUSDT", "buy_price": 100.0, "quantity": 1.0
        })
        assert created.status_code == 200
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT include_ai_analysis, preferences_customized FROM positions ORDER BY id")
        assert await cursor.fetchall() == [(int(include_ai), 0), (int(include_ai), 0)]


@pytest.mark.asyncio
async def test_edited_settings_survive_close_and_reimport(tmp_path, monkeypatch):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    await main.perform_binance_sync()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        positions = (await client.get("/api/positions")).json()
        updated = await client.put(f"/api/positions/{positions[0]['id']}", json={
            "check_interval_hours": 8.0, "include_ai_analysis": False
        })
        assert updated.status_code == 200
    holdings = main.binance_client.get_account_balances
    monkeypatch.setattr(main.binance_client, "get_account_balances", AsyncMock(return_value=[]))
    assert (await main.perform_binance_sync())["deactivated"] == ["TSLABUSDT"]
    monkeypatch.setattr(main.binance_client, "get_account_balances", holdings)
    await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("""
            SELECT check_interval_hours, include_ai_analysis FROM positions WHERE is_active = 1
        """)
        assert await cursor.fetchall() == [(8.0, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_wallet", ["spot", "funding"])
async def test_failed_account_read_does_not_deactivate_positions(tmp_path, monkeypatch, failing_wallet):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            INSERT INTO positions (symbol, buy_price, quantity, check_interval_hours)
            VALUES ('TSLABUSDT', 90, 1, 4)
        """)
        await db.commit()

    client = BinanceClient(api_key="test-key", api_secret="test-secret")
    monkeypatch.setattr(main.binance_client, "get_account_balances", client.get_account_balances)
    status_spot = 401 if failing_wallet == "spot" else 200
    status_funding = 403 if failing_wallet == "funding" else 200
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=httpx.Response(
        status_spot, json={"code": -2015} if status_spot != 200 else {"balances": []}
    )))
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(
        status_funding, json={"code": -2015} if status_funding != 200 else []
    )))
    with pytest.raises(RuntimeError):
        await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT quantity, check_interval_hours, is_active FROM positions")
        assert await cursor.fetchall() == [(1.0, 4.0, 1)]


@pytest.mark.asyncio
async def test_failed_price_read_does_not_deactivate_position(tmp_path, monkeypatch):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            INSERT INTO positions (symbol, buy_price, quantity, check_interval_hours)
            VALUES ('TSLABUSDT', 90, 1, 4)
        """)
        await db.commit()
    monkeypatch.setattr(main.binance_client, "get_ticker_price", AsyncMock(side_effect=RuntimeError("timeout")))
    with pytest.raises(RuntimeError):
        await main.perform_binance_sync()
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT check_interval_hours, is_active FROM positions")
        assert await cursor.fetchall() == [(4.0, 1)]


@pytest.mark.asyncio
async def test_explicit_interval_overrides_default(tmp_path, monkeypatch):
    path = await prepare_db(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/positions", json={
            "symbol": "TSLABUSDT", "buy_price": 90, "quantity": 1, "check_interval_hours": 0
        })
        assert response.status_code == 200
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute("SELECT check_interval_hours FROM positions")
        assert await cursor.fetchone() == (0.0,)


@pytest.mark.asyncio
async def test_confirmed_empty_balance_still_closes_position(tmp_path, monkeypatch):
    path = await prepare_db(tmp_path, monkeypatch)
    mock_holdings(monkeypatch)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO positions (symbol, buy_price, quantity) VALUES ('TSLABUSDT', 90, 1)")
        await db.commit()
    monkeypatch.setattr(main.binance_client, "get_account_balances", AsyncMock(return_value=[]))
    assert (await main.perform_binance_sync())["deactivated"] == ["TSLABUSDT"]


@pytest.mark.asyncio
async def test_daily_schedule_explicitly_pushes_only_once(tmp_path, monkeypatch):
    from datetime import timezone
    runner, notifier, path = await short_term_runner(tmp_path, monkeypatch)
    async with aiosqlite.connect(path) as db:
        await db.executemany('INSERT INTO system_configs (key,value) VALUES (?,?)', [
            ('ai_picks_push_enabled','1'), ('ai_picks_schedule_times','21:30')])
        await db.commit()
    now = datetime(2026,10,2,13,31,tzinfo=timezone.utc)  # 21:31 Asia/Singapore
    await main.scheduled_analysis_tick(now)
    count = len(notifier.sent)
    assert count > 0
    await main.scheduled_analysis_tick(now)
    assert len(notifier.sent) == count
    runs = await runner.store.list_runs()
    assert len(runs) == 1 and runs[0]['run_key'] == 'schedule:2026-10-02:21:30'


@pytest.mark.asyncio
async def test_scheduled_slot_with_fast_failing_agent_runs_once_and_reports_failure(tmp_path, monkeypatch):
    from datetime import timedelta, timezone
    from backend.app.services.short_term.agent_client import AgentResult
    runner, notifier, path = await short_term_runner(tmp_path, monkeypatch)
    err = AgentResult(status='error', error_code='auth_expired', error_message='x')
    runner.agent.results = {'NVDA': err, 'TSLA': err}
    async with aiosqlite.connect(path) as db:
        await db.executemany('INSERT INTO system_configs (key,value) VALUES (?,?)', [
            ('ai_picks_push_enabled','1'), ('ai_picks_schedule_times','21:30')])
        await db.commit()
    start = datetime(2026,10,2,13,31,tzinfo=timezone.utc)  # 21:31 Asia/Singapore
    for minutes in range(0, 150, 10):  # ticks through the 3 h catch-up window, each past the 5-minute guard
        await main.scheduled_analysis_tick(start + timedelta(minutes=minutes))
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        await runner.store.execute('UPDATE analysis_runs SET finished_at=? WHERE finished_at IS NOT NULL', (old,))
    runs = await runner.store.list_runs()
    assert len(runs) == 1 and runs[0]['status'] == 'failed' and '凭据已过期' in runs[0]['message']
    assert len(runner.agent.calls) == 2  # one attempt per candidate, no auto-retry of the slot
    assert await runner.store.sessions_started_since('') == 2
    assert len(notifier.sent) == 1 and '本轮分析未完成' in notifier.sent[0][1]

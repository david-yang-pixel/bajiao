import json
import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from backend.app.main import app
from backend.app.database import init_db

@pytest.mark.asyncio
async def test_health_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/api/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "service": "binance-bstocks-bot"}

@pytest.mark.asyncio
async def test_symbols_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/api/bstocks/symbols")
        assert res.status_code == 200
        data = res.json()
        assert "symbols" in data
        assert "NVDABUSDT" in data["symbols"]

@pytest.mark.asyncio
async def test_positions_crud(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_api.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Create Position
        payload = {
            "symbol": "AAPLB",
            "buy_price": 180.0,
            "quantity": 10.0,
            "take_profit_price": 200.0,
            "stop_loss_price": 170.0,
            "check_interval_hours": 2,
            "ladder_alerts": [5.0, 10.0]
        }
        res = await ac.post("/api/positions", json=payload)
        assert res.status_code == 200
        pos_id = res.json()["id"]

        # 2. List Positions with mock price
        with patch("backend.app.main.binance_client.get_ticker_price", new_callable=AsyncMock) as mock_price:
            mock_price.return_value = 198.0  # +10%
            list_res = await ac.get("/api/positions")
            assert list_res.status_code == 200
            positions = list_res.json()
            assert len(positions) == 1
            assert positions[0]["symbol"] == "AAPLB"
            assert positions[0]["current_price"] == 198.0
            assert positions[0]["pnl_pct"] == 10.0

        # 3. Delete Position
        del_res = await ac.delete(f"/api/positions/{pos_id}")
        assert del_res.status_code == 200

        # 4. Verify inactive
        list_res2 = await ac.get("/api/positions")
        assert len(list_res2.json()) == 0

@pytest.mark.asyncio
async def test_system_config_endpoints(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_cfg.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Get initial config
        res = await ac.get("/api/config")
        assert res.status_code == 200
        data = res.json()
        assert "binance_mcp_url" in data

        # 2. Save config
        payload = {
            "openai_api_key": "sk-test-openai-key-999",
            "openai_model": "deepseek-chat",
            "openai_base_url": "https://api.deepseek.com/v1",
            "feishu_webhook_url": "https://open.feishu.cn/test",
            "wecom_webhook_url": "https://qyapi.weixin.qq.com/test"
        }
        post_res = await ac.post("/api/config", json=payload)
        assert post_res.status_code == 200

        with patch("backend.app.services.binance_client.BinanceClient.get_account_balances", new_callable=AsyncMock) as balances:
            balances.return_value = []
            credentials = await ac.post("/api/config/binance-credentials", json={
                "api_key": "A" * 64, "api_secret": "B" * 64,
            })
            assert credentials.status_code == 200

        # 3. Read back masked
        res2 = await ac.get("/api/config")
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["has_binance_key"] is True
        assert data2["has_binance_secret"] is True
        assert data2["has_openai_key"] is True
        assert "******" in data2["binance_api_key"]
        assert data2["binance_api_secret"] == ""
        assert data2["openai_model"] == "deepseek-chat"

@pytest.mark.asyncio
async def test_position_update_and_sync(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_update_sync.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create position
        res = await ac.post("/api/positions", json={
            "symbol": "AMZNBUSDT",
            "buy_price": 240.0,
            "quantity": 0.5,
            "take_profit_price": 270.0,
            "stop_loss_price": 220.0
        })
        assert res.status_code == 200
        pos_id = res.json()["id"]

        # Update position
        up_res = await ac.put(f"/api/positions/{pos_id}", json={
            "buy_price": 248.33,
            "quantity": 0.65787882,
            "take_profit_price": 280.0
        })
        assert up_res.status_code == 200

        # Verify update
        with patch("backend.app.main.binance_client.get_ticker_price", new_callable=AsyncMock) as mock_p:
            mock_p.return_value = 249.47
            get_res = await ac.get("/api/positions")
            assert get_res.status_code == 200
            data = get_res.json()
            assert len(data) == 1
            assert data[0]["buy_price"] == 248.33
            assert data[0]["quantity"] == 0.65787882
            assert data[0]["take_profit_price"] == 280.0

        # Test sync with funding wallet asset
        with patch("backend.app.main.binance_client.api_key", "test-key"), \
             patch("backend.app.main.binance_client.api_secret", "test-secret"), \
             patch("backend.app.main.binance_client.get_account_balances", new_callable=AsyncMock) as mock_b, \
             patch("backend.app.main.binance_client.get_ticker_price", new_callable=AsyncMock) as mock_tp, \
             patch("backend.app.main.binance_client.get_average_buy_price", new_callable=AsyncMock) as mock_avg:
            
            mock_b.return_value = [
                {"asset": "AMZNB", "free": 0.65787882, "locked": 0.0, "total": 0.65787882, "wallet": "资金账户"},
                {"asset": "MRVLB", "free": 0.000293, "locked": 0.0, "total": 0.000293, "wallet": "现货账户"}
            ]
            mock_tp.return_value = 249.47
            mock_avg.return_value = None # Should preserve existing 248.33!

            sync_res = await ac.post("/api/positions/sync-binance")
            assert sync_res.status_code == 200
            sync_data = sync_res.json()
            assert sync_data["success"] is True
            assert sync_data["count"] == 1
            assert sync_data["synced"][0]["symbol"] == "AMZNBUSDT"
            assert sync_data["synced"][0]["buy_price"] == 248.33  # Preserved!
            assert sync_data["synced"][0]["wallet"] == "资金账户"

@pytest.mark.asyncio
async def test_position_test_alert(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_alert_endpoint.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        create_res = await ac.post("/api/positions", json={
            "symbol": "AMZNBUSDT",
            "buy_price": 248.33,
            "quantity": 0.6578,
            "check_interval_hours": 0.5
        })
        pos_id = create_res.json()["id"]

        with patch("backend.app.main.binance_client.get_ticker_price", new_callable=AsyncMock) as mock_p, \
             patch("backend.app.main.notification_service.send_position_alert", new_callable=AsyncMock) as mock_alert:
            mock_p.return_value = 249.50
            mock_alert.return_value = True

            test_res = await ac.post(f"/api/positions/{pos_id}/test-alert")
            assert test_res.status_code == 200
            assert test_res.json()["success"] is True
            assert "periodic" in test_res.json()["triggered"]
            mock_alert.assert_called_once()

@pytest.mark.asyncio
async def test_daily_picks_lifecycle_and_clear(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_picks.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    async with aiosqlite.connect(db_file) as db:
        await db.execute("INSERT INTO daily_picks (symbol, reason, target_price) VALUES (?, ?, ?)", ("AAPLBUSDT", "突破", 250.0))
        await db.execute("INSERT INTO daily_picks (symbol, reason, target_price) VALUES (?, ?, ?)", ("TSLABUSDT", "放量", 300.0))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        picks_res = await ac.get("/api/analysis/picks")
        assert len(picks_res.json()) == 2
        pick_id = picks_res.json()[0]["id"]

        # Delete single pick
        del_one = await ac.delete(f"/api/analysis/picks/{pick_id}")
        assert del_one.status_code == 200

        picks_res2 = await ac.get("/api/analysis/picks")
        assert len(picks_res2.json()) == 1

        # Clear all picks
        del_all = await ac.delete("/api/analysis/picks")
        assert del_all.status_code == 200

        picks_res3 = await ac.get("/api/analysis/picks")
        assert len(picks_res3.json()) == 0

@pytest.mark.asyncio
async def test_position_with_include_ai_analysis_alert(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_ai_pos.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create position with include_ai_analysis=True
        create_res = await ac.post("/api/positions", json={
            "symbol": "NVDAUSDT",
            "buy_price": 120.0,
            "quantity": 10.0,
            "check_interval_hours": 0.5,
            "include_ai_analysis": True
        })
        assert create_res.status_code == 200
        pos_id = create_res.json()["id"]

        mock_analysis = {
            "symbol": "NVDAUSDT",
            "reason": "突破均线，短线量价配合良好",
            "target_price": 128.0,
            "stop_loss_price": 116.0
        }

        with patch("backend.app.main.binance_client.get_ticker_price", new_callable=AsyncMock) as mock_p, \
             patch("backend.app.main.position_monitor.get_symbol_trend_analysis", new_callable=AsyncMock) as mock_trend, \
             patch("backend.app.main.notification_service.send_position_alert", new_callable=AsyncMock) as mock_alert:
            mock_p.return_value = 122.50
            mock_trend.return_value = mock_analysis
            mock_alert.return_value = True

            test_res = await ac.post(f"/api/positions/{pos_id}/test-alert")
            assert test_res.status_code == 200
            assert test_res.json()["success"] is True

            # Verify get_symbol_trend_analysis was called
            mock_trend.assert_called_once_with("NVDAUSDT")
            # Verify notification_service was called with trend_analysis
            mock_alert.assert_called_once()
            call_kwargs = mock_alert.call_args[1]
            assert call_kwargs["trend_analysis"] == mock_analysis

        # Test updating include_ai_analysis to False
        up_res = await ac.put(f"/api/positions/{pos_id}", json={
            "include_ai_analysis": False
        })
        assert up_res.status_code == 200

        # Verify it is now False
        get_res = await ac.get("/api/positions")
        assert get_res.status_code == 200
        assert get_res.json()[0]["include_ai_analysis"] in (0, False)

@pytest.mark.asyncio
async def test_system_config_ai_picks_push_schedule(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_cfg_sched.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Default config
        cfg_res = await ac.get("/api/config")
        assert cfg_res.status_code == 200
        assert cfg_res.json()["ai_picks_push_enabled"] is False
        assert cfg_res.json()["ai_picks_push_time"] == "21:30"

        # Update schedule config
        save_res = await ac.post("/api/config", json={
            "ai_picks_push_enabled": True,
            "ai_picks_push_time": "21:20"
        })
        assert save_res.status_code == 200

        # Re-fetch
        cfg_res2 = await ac.get("/api/config")
        assert cfg_res2.status_code == 200
        assert cfg_res2.json()["ai_picks_push_enabled"] is True
        assert cfg_res2.json()["ai_picks_push_time"] == "21:20"

@pytest.mark.asyncio
async def test_feishu_events_endpoint(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_feishu_events.db")
    monkeypatch.setattr("backend.app.config.settings.DB_PATH", db_file)
    await init_db(db_file)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. URL Challenge verification
        challenge_res = await ac.post("/api/feishu/events", json={
            "type": "url_verification",
            "token": "test_token",
            "challenge": "challenge_12345"
        })
        assert challenge_res.status_code == 200
        assert challenge_res.json() == {"challenge": "challenge_12345"}

        # 2. Message event webhook
        with patch("backend.app.main.feishu_bot_service.handle_incoming_text", new_callable=AsyncMock) as mock_handle:
            event_payload = {
                "schema": "2.0",
                "header": {
                    "event_id": "ev_123456",
                    "event_type": "im.message.receive_v1"
                },
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_user1"}},
                    "message": {
                        "message_id": "om_msg123",
                        "chat_id": "oc_chat123",
                        "content": json.dumps({"text": "持仓"})
                    }
                }
            }
            msg_res = await ac.post("/api/feishu/events", json=event_payload)
            assert msg_res.status_code == 200
            assert msg_res.json() == {"code": 0}
            mock_handle.assert_called_once_with("持仓", "om_msg123", "oc_chat123", "ou_user1")




import hashlib
import hmac
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from backend.app import main
from backend.app.database import init_db
from backend.app.services.binance_client import BinanceClient

OLD_KEY = "A" * 64
OLD_SECRET = "B" * 64
NEW_KEY = "C" * 64
NEW_SECRET = "D" * 64
REAL_APPLY_RUNTIME_CONFIG = main.apply_runtime_config


@pytest_asyncio.fixture
async def config_db(tmp_path, monkeypatch):
    path = str(tmp_path / "credentials.db")
    monkeypatch.setattr(main.settings, "DB_PATH", path)
    monkeypatch.setattr(main, "apply_runtime_config", AsyncMock())
    monkeypatch.setattr(main.binance_client, "api_key", OLD_KEY)
    monkeypatch.setattr(main.binance_client, "api_secret", OLD_SECRET)
    await init_db(path)
    async with aiosqlite.connect(path) as db:
        await db.executemany("INSERT INTO system_configs (key, value) VALUES (?, ?)", [
            ("binance_api_key", OLD_KEY), ("binance_api_secret", OLD_SECRET),
            ("default_check_interval_hours", "4.0"),
        ])
        await db.commit()
    return path


async def stored_config(path):
    async with aiosqlite.connect(path) as db:
        return dict(await (await db.execute("SELECT key, value FROM system_configs")).fetchall())


def api_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


def remote_api_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=("203.0.113.10", 1234)), base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["shortValue", NEW_SECRET])
async def test_general_config_cannot_replace_binance_credentials(config_db, secret):
    async with api_client() as client:
        response = await client.post("/api/config", json={
            "binance_api_secret": secret, "default_check_interval_hours": 8,
        })
    assert response.status_code == 400
    cfg = await stored_config(config_db)
    assert cfg["binance_api_key"] == OLD_KEY
    assert cfg["binance_api_secret"] == OLD_SECRET
    assert cfg["default_check_interval_hours"] == "4.0"


@pytest.mark.asyncio
async def test_masked_or_absent_fields_preserve_credentials(config_db):
    async with api_client() as client:
        for payload in [
            {"default_check_interval_hours": 8},
            {"binance_api_key": "AAAA******AAAA", "binance_api_secret": "BBBB******BBBB", "default_check_interval_hours": 4},
            {"binance_api_key": "", "binance_api_secret": "", "default_check_interval_hours": 0},
        ]:
            assert (await client.post("/api/config", json=payload)).status_code == 200
    cfg = await stored_config(config_db)
    assert cfg["binance_api_key"] == OLD_KEY
    assert cfg["binance_api_secret"] == OLD_SECRET
    assert float(cfg["default_check_interval_hours"]) == 0


@pytest.mark.asyncio
async def test_secret_is_never_returned_to_the_form(config_db):
    async with api_client() as client:
        response = await client.get("/api/config")
    assert response.json()["binance_api_secret"] == ""
    assert response.json()["has_binance_secret"] is True
    assert OLD_SECRET not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"api_key": NEW_KEY, "api_secret": "shortValue"},
    {"api_key": NEW_KEY},
    {"api_key": "bad", "api_secret": NEW_SECRET},
    {"api_key": "***" + "C" * 61, "api_secret": NEW_SECRET},
])
async def test_invalid_pair_is_rejected_without_echoing_secrets(config_db, payload):
    async with api_client() as client:
        response = await client.post("/api/config/binance-credentials", json=payload)
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]
    assert NEW_SECRET not in response.text
    assert "shortValue" not in response.text
    assert (await stored_config(config_db))["binance_api_secret"] == OLD_SECRET


@pytest.mark.asyncio
@pytest.mark.parametrize("wallet", ["spot", "funding"])
async def test_failed_account_validation_preserves_both_old_values(config_db, monkeypatch, wallet, caplog):
    unsafe_message = "sensitive provider message " + NEW_SECRET
    get = AsyncMock(return_value=httpx.Response(
        400 if wallet == "spot" else 200,
        json={"code": -1022, "msg": unsafe_message} if wallet == "spot" else {"balances": []},
    ))
    post = AsyncMock(return_value=httpx.Response(400, json={"code": -1022, "msg": unsafe_message}))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    async with api_client() as client:
        response = await client.request("POST", "/api/config/binance-credentials", json={
            "api_key": NEW_KEY, "api_secret": NEW_SECRET,
        })
    assert response.status_code == 400
    assert "-1022" in response.text
    assert NEW_SECRET not in response.text + caplog.text
    assert unsafe_message not in response.text + caplog.text
    cfg = await stored_config(config_db)
    assert cfg["binance_api_key"] == OLD_KEY
    assert cfg["binance_api_secret"] == OLD_SECRET
    assert main.apply_runtime_config.await_count == 0


@pytest.mark.asyncio
async def test_valid_pair_is_saved_atomically_and_reloaded(config_db, monkeypatch):
    validation = AsyncMock(return_value=[])
    monkeypatch.setattr(BinanceClient, "get_account_balances", validation)
    async with api_client() as client:
        response = await client.post("/api/config/binance-credentials", json={
            "api_key": NEW_KEY, "api_secret": NEW_SECRET,
        })
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert NEW_KEY not in response.text and NEW_SECRET not in response.text
    cfg = await stored_config(config_db)
    assert cfg["binance_api_key"] == NEW_KEY
    assert cfg["binance_api_secret"] == NEW_SECRET
    assert cfg["default_check_interval_hours"] == "4.0"
    validation.assert_awaited_once()
    main.apply_runtime_config.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", [None, "incorrect", "错误密钥"])
async def test_remote_update_requires_current_secret(config_db, monkeypatch, proof):
    validate = AsyncMock(return_value=[])
    monkeypatch.setattr(BinanceClient, "get_account_balances", validate)
    async with remote_api_client() as client:
        response = await client.post("/api/config/binance-credentials", json={
            "api_key": NEW_KEY, "api_secret": NEW_SECRET, "current_api_secret": proof,
        })
    assert response.status_code == 403
    assert (await stored_config(config_db))["binance_api_secret"] == OLD_SECRET
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_owner_can_update_a_verified_pair(config_db, monkeypatch):
    monkeypatch.setattr(BinanceClient, "get_account_balances", AsyncMock(return_value=[]))
    async with remote_api_client() as client:
        response = await client.post("/api/config/binance-credentials", json={
            "api_key": NEW_KEY, "api_secret": NEW_SECRET, "current_api_secret": OLD_SECRET,
        })
    assert response.status_code == 200
    assert (await stored_config(config_db))["binance_api_secret"] == NEW_SECRET


@pytest.mark.asyncio
async def test_proxy_headers_cannot_bypass_owner_verification(config_db):
    async with api_client() as client:
        response = await client.post("/api/config/binance-credentials", headers={"X-Forwarded-For": "203.0.113.10"}, json={
            "api_key": NEW_KEY, "api_secret": NEW_SECRET,
        })
    assert response.status_code == 403
    assert (await stored_config(config_db))["binance_api_secret"] == OLD_SECRET


@pytest.mark.asyncio
async def test_update_does_not_overwrite_another_change_during_validation(config_db, monkeypatch):
    async def competing_update(*args):
        async with aiosqlite.connect(config_db) as db:
            await db.execute("UPDATE system_configs SET value=? WHERE key='binance_api_secret'", ("E" * 64,))
            await db.commit()
        return []
    monkeypatch.setattr(BinanceClient, "get_account_balances", competing_update)
    async with api_client() as client:
        response = await client.post("/api/config/binance-credentials", json={"api_key": NEW_KEY, "api_secret": NEW_SECRET})
    assert response.status_code == 409
    cfg = await stored_config(config_db)
    assert cfg["binance_api_key"] == OLD_KEY
    assert cfg["binance_api_secret"] == "E" * 64


@pytest.mark.asyncio
async def test_restart_uses_persisted_pair_over_environment(config_db, monkeypatch):
    monkeypatch.setattr(main.settings, "BINANCE_API_KEY", "E" * 64)
    monkeypatch.setattr(main.settings, "BINANCE_API_SECRET", "F" * 64)
    monkeypatch.setattr(main.feishu_bot_service, "stop", lambda: None)
    monkeypatch.setattr(main.feishu_bot_service, "set_credentials", lambda *args: None)
    monkeypatch.setattr(main.feishu_bot_service, "start", lambda: None)
    await init_db(config_db)
    await REAL_APPLY_RUNTIME_CONFIG()
    assert main.binance_client.api_key == OLD_KEY
    assert main.binance_client.api_secret == OLD_SECRET


@pytest.mark.asyncio
async def test_connection_checks_saved_accounts_without_writing(config_db, monkeypatch):
    validation = AsyncMock(side_effect=RuntimeError("provider network failure"))
    monkeypatch.setattr(BinanceClient, "get_account_balances", validation)
    async with api_client() as client:
        response = await client.post("/api/config/test", json={"service": "binance"})
    assert response.status_code == 200
    assert response.json()["success"] is False
    validation.assert_awaited_once()
    assert (await stored_config(config_db))["binance_api_secret"] == OLD_SECRET


@pytest.mark.asyncio
async def test_balances_use_one_pair_even_during_a_runtime_update(monkeypatch):
    binance = BinanceClient(api_key=OLD_KEY, api_secret=OLD_SECRET)
    async def spot(*args, **kwargs):
        binance.api_key, binance.api_secret = NEW_KEY, NEW_SECRET
        return httpx.Response(200, json={"balances": []})
    funding = AsyncMock(return_value=httpx.Response(200, json=[]))
    monkeypatch.setattr(httpx.AsyncClient, "get", spot)
    monkeypatch.setattr(httpx.AsyncClient, "post", funding)
    assert await binance.get_account_balances() == []
    kwargs = funding.call_args.kwargs
    assert kwargs["headers"]["X-MBX-APIKEY"] == OLD_KEY
    params = dict(kwargs["params"])
    signature = params.pop("signature")
    assert signature == hmac.new(OLD_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()

import os
import stat

import httpx
import pytest
from fastapi import FastAPI

from backend.app.config import settings
from backend.app.services.short_term.agent_token import get_or_create_token
from backend.app.services.short_term.internal_api import InternalDeps, build_internal_router
from backend.app.services.short_term.universe import MarketDataUnavailable

TOKEN = 'secret-token'
H = {'Authorization': f'Bearer {TOKEN}'}


def make_deps(**over):
    async def factors(symbol):
        if symbol == 'ZZZ':
            raise LookupError(symbol)
        return {'symbol': symbol, 'rsi': 50}

    async def token_quote(symbol):
        return {'symbol': symbol, 'price_usdt': 1.5}

    async def crypto():
        return {'btc': 1}

    async def market():
        return {'spy': 2}

    async def cfg():
        return {'alpaca_api_key': 'a', 'alpaca_secret_key': 'b', 'fred_api_key': 'c', 'model': 'm'}

    d = dict(token_provider=lambda: TOKEN, factors=factors, token_quote=token_quote,
             crypto_context=crypto, market_context=market, agent_config=cfg)
    d.update(over)
    return InternalDeps(**d)


def client(deps=None):
    app = FastAPI()
    app.include_router(build_internal_router(deps or make_deps()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t')


def test_token_file_created_and_stable(tmp_path):
    d = tmp_path / 'sub' / 'agent'
    t = get_or_create_token(str(d))
    assert len(t) >= 32
    assert get_or_create_token(str(d)) == t
    assert stat.S_IMODE(os.stat(d / 'internal_token').st_mode) == 0o600
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


@pytest.mark.asyncio
async def test_requires_bearer():
    async with client() as c:
        assert (await c.get('/internal/agent-config')).status_code == 401
        r = await c.get('/internal/agent-config', headers={'Authorization': 'Bearer nope'})
        assert r.status_code == 401
        r = await c.get('/internal/agent-config', headers={'Authorization': 'Basic abc'})
        assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('hdr', ['X-Forwarded-For', 'Forwarded', 'X-Real-IP'])
async def test_proxy_headers_forbidden(hdr):
    async with client() as c:
        r = await c.get('/internal/agent-config', headers={**H, hdr: '1.2.3.4'})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_routes_return_dep_data():
    async with client() as c:
        assert (await c.get('/internal/agent-config', headers=H)).json()['model'] == 'm'
        r = await c.post('/internal/tools/get_factors', json={'symbol': 'NVDA'}, headers=H)
        assert r.json() == {'symbol': 'NVDA', 'rsi': 50}
        r = await c.post('/internal/tools/get_token_quote', json={'symbol': 'BRK.B'}, headers=H)
        assert r.json()['price_usdt'] == 1.5
        assert (await c.post('/internal/tools/get_crypto_context', json={}, headers=H)).json() == {'btc': 1}
        assert (await c.post('/internal/tools/get_market_context', json={}, headers=H)).json() == {'spy': 2}


@pytest.mark.asyncio
async def test_token_provider_called_per_request():
    box = {'t': 'a'}
    async with client(make_deps(token_provider=lambda: box['t'])) as c:
        assert (await c.get('/internal/agent-config', headers={'Authorization': 'Bearer b'})).status_code == 401
        box['t'] = 'b'
        assert (await c.get('/internal/agent-config', headers={'Authorization': 'Bearer b'})).status_code == 200


@pytest.mark.asyncio
async def test_failures_map_to_503_and_404():
    async def boom(*a):
        raise MarketDataUnavailable('x')

    async def bad(*a):
        raise ValueError('坏值')

    async with client(make_deps(factors=boom, crypto_context=bad)) as c:
        r = await c.post('/internal/tools/get_factors', json={'symbol': 'NVDA'}, headers=H)
        assert r.status_code == 503 and r.json() == {'detail': 'x'}
        r = await c.post('/internal/tools/get_crypto_context', json={}, headers=H)
        assert r.status_code == 503 and r.json()['detail'] == '坏值'
    async with client() as c:
        r = await c.post('/internal/tools/get_factors', json={'symbol': 'ZZZ'}, headers=H)
        assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('sym', ['nvda', 'TOOLONGSYMBOL1', '', 'A B', 'N1'])
async def test_bad_symbol_422(sym):
    async with client() as c:
        r = await c.post('/internal/tools/get_factors', json={'symbol': sym}, headers=H)
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_main_middleware_skips_basic_auth_for_internal(monkeypatch, tmp_path):
    from backend.app.main import app
    monkeypatch.setattr(settings, 'ADMIN_PASSWORD', 'pw')
    monkeypatch.setattr(settings, 'AGENT_DATA_DIR', str(tmp_path / 'agent'))
    token = get_or_create_token(str(tmp_path / 'agent'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        # The internal router (mounted in main) answers with its own bearer check, not Basic auth.
        r = await c.get('/internal/agent-config')
        assert r.status_code == 401 and r.json()['detail'] == '内网令牌无效'
        r = await c.get('/internal/agent-config', headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 200 and set(r.json()) == {'alpaca_api_key', 'alpaca_secret_key', 'fred_api_key', 'model'}
        assert (await c.get('/api/stocks')).status_code == 401

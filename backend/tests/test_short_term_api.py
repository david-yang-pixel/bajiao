import json
import os
import stat
from datetime import datetime, timezone

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from backend.app import main
from backend.app.database import init_db
from backend.app.services.analysis_store import AnalysisStore
from backend.app.services.short_term.universe import MarketDataUnavailable, TokenPair


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'st.db')
    monkeypatch.setattr(main.settings, 'DB_PATH', path)
    monkeypatch.setattr(main.settings, 'AGENT_DATA_DIR', str(tmp_path / 'agent'))
    monkeypatch.setattr(main.feishu_bot_service, 'set_credentials', lambda *a: None)
    monkeypatch.setattr(main.feishu_bot_service, 'start', lambda: None)
    monkeypatch.setattr(main.feishu_bot_service, 'stop', lambda: None)
    await init_db(path)
    return path


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')


async def put_config(path, **values):
    async with aiosqlite.connect(path) as conn:
        await conn.executemany('INSERT INTO system_configs(key,value) VALUES(?,?)', list(values.items()))
        await conn.commit()


async def config_rows(path):
    async with aiosqlite.connect(path) as conn:
        return (await (await conn.execute('SELECT COUNT(*) FROM system_configs')).fetchone())[0]


class FakeShortTerm:
    """Records scheduled calls; the DB claim is the real AnalysisStore.begin_run."""

    def __init__(self):
        self.store = AnalysisStore()
        self.calls, self.analyzed = [], []

    async def run(self, top_n=5, push_notifications=False, run_key=None):
        self.calls.append((top_n, push_notifications, run_key))
        run_id, claimed = await self.store.begin_run(run_key, push_notifications)
        if claimed:
            self.analyzed.append(run_key)
            await self.store.finish_run(run_id, 'completed', [], [], [], 'ok')
        return {'run_id': run_id}


# ---- schedule ----

@pytest.mark.asyncio
async def test_schedule_runs_slot_once_within_catchup_window(db, monkeypatch):
    fake = FakeShortTerm()
    monkeypatch.setattr(main, 'short_term', fake)
    await put_config(db, ai_picks_push_enabled='1', analysis_top_n='3')
    assert await main.scheduled_analysis_tick(utc(2026, 11, 17, 0, 59)) is None  # 08:59 SGT
    assert fake.calls == []
    await main.scheduled_analysis_tick(utc(2026, 11, 17, 1, 5))  # 09:05 SGT
    assert fake.calls == [(3, True, 'schedule:2026-11-17:09:00')]
    await main.scheduled_analysis_tick(utc(2026, 11, 17, 1, 30))  # same key again
    assert fake.analyzed == ['schedule:2026-11-17:09:00']
    calls = len(fake.calls)
    assert await main.scheduled_analysis_tick(utc(2026, 11, 17, 4, 30)) is None  # 12:30 SGT: no catch-up
    assert len(fake.calls) == calls
    await main.scheduled_analysis_tick(utc(2026, 11, 17, 13, 0))  # 21:00 SGT
    assert fake.analyzed[-1] == 'schedule:2026-11-17:21:00'


@pytest.mark.asyncio
async def test_schedule_disabled_custom_times_timezone_and_midnight(db, monkeypatch):
    fake = FakeShortTerm()
    monkeypatch.setattr(main, 'short_term', fake)
    assert await main.scheduled_analysis_tick(utc(2026, 11, 17, 1, 5)) is None
    assert fake.calls == []
    await put_config(db, ai_picks_push_enabled='1', ai_picks_schedule_times='23:30',
                     ai_picks_push_timezone='America/New_York')
    # 2026-11-18 01:00 New York (UTC-5) is within 3h of the previous day's 23:30 slot.
    await main.scheduled_analysis_tick(utc(2026, 11, 18, 6, 0))
    assert fake.calls == [(5, True, 'schedule:2026-11-17:23:30')]


# ---- config ----

@pytest.mark.asyncio
async def test_config_defaults_without_writing_rows(db):
    async with client() as c:
        cfg = (await c.get('/api/config')).json()
    assert cfg['ai_picks_schedule_times'] == '09:00,21:00'
    assert cfg['ai_picks_push_timezone'] == 'Asia/Singapore'
    assert cfg['analysis_top_n'] == 5 and cfg['agent_daily_session_limit'] == 24
    assert cfg['has_alpaca_key'] is False and cfg['has_fred_key'] is False and cfg['agent_model'] == ''
    for old in ('fmp_api_key', 'has_fmp_key', 'analysis_min_score', 'earnings_blackout_days', 'ai_picks_push_time'):
        assert old not in cfg
    assert await config_rows(db) == 0


@pytest.mark.asyncio
async def test_config_saves_alpaca_masked_and_reloads_runtime(db, monkeypatch):
    monkeypatch.setattr(main.alpaca_data, 'api_key', '')
    monkeypatch.setattr(main.alpaca_data, 'secret_key', '')
    monkeypatch.setattr(main.short_term, 'session_limit', 24)
    main.short_term._position_cache['X'] = (0.0, {})
    async with client() as c:
        r = await c.post('/api/config', json={
            'alpaca_api_key': 'AKTEST1234567890', 'alpaca_secret_key': 'alpaca-secret-value',
            'fred_api_key': 'fred-secret-value', 'agent_model': 'gpt-5.1-codex',
            'ai_picks_schedule_times': '21:00, 09:00', 'analysis_top_n': 2, 'agent_daily_session_limit': 30,
            'fmp_api_key': 'legacy-fmp'})
        assert r.status_code == 200
        cfg = (await c.get('/api/config')).json()
        assert cfg['alpaca_api_key'] == 'AKTE******7890' and cfg['has_alpaca_key'] is True
        assert cfg['alpaca_secret_key'] == '' and cfg['fred_api_key'] == '' and cfg['has_fred_key'] is True
        assert 'alpaca-secret-value' not in json.dumps(cfg) and 'fred-secret-value' not in json.dumps(cfg)
        assert cfg['ai_picks_schedule_times'] == '09:00,21:00'
        assert cfg['analysis_top_n'] == 2 and cfg['agent_daily_session_limit'] == 30
        assert cfg['agent_model'] == 'gpt-5.1-codex'
        assert main.alpaca_data.api_key == 'AKTEST1234567890' and main.alpaca_data.secret_key == 'alpaca-secret-value'
        assert main.short_term.session_limit == 30 and main.short_term._position_cache == {}
        assert await main.short_term.agent_config() == {
            'alpaca_api_key': 'AKTEST1234567890', 'alpaca_secret_key': 'alpaca-secret-value',
            'fred_api_key': 'fred-secret-value', 'model': 'gpt-5.1-codex'}
        # Masked / empty resubmits keep the stored secrets.
        assert (await c.post('/api/config', json={'alpaca_api_key': cfg['alpaca_api_key'],
                                                    'alpaca_secret_key': '', 'fred_api_key': None})).status_code == 200
    async with aiosqlite.connect(db) as conn:
        stored = dict(await (await conn.execute('SELECT key,value FROM system_configs')).fetchall())
    assert stored['alpaca_api_key'] == 'AKTEST1234567890' and stored['alpaca_secret_key'] == 'alpaca-secret-value'
    assert stored['fred_api_key'] == 'fred-secret-value' and 'fmp_api_key' not in stored


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'ai_picks_schedule_times': '9:00'}, {'ai_picks_schedule_times': '09:00,25:00'},
    {'ai_picks_schedule_times': ' , '}, {'analysis_top_n': 0}, {'analysis_top_n': 6},
    {'agent_daily_session_limit': 0}, {'agent_daily_session_limit': 101},
    {'ai_picks_push_timezone': 'Not/AZone'},
])
async def test_invalid_config_rejected_atomically_without_echo(db, payload):
    async with client() as c:
        r = await c.post('/api/config', json={**payload, 'alpaca_secret_key': 'do-not-echo'})
    assert r.status_code == 422 and 'do-not-echo' not in r.text
    assert await config_rows(db) == 0


# ---- credentials ----

@pytest.mark.asyncio
@pytest.mark.parametrize('body', [b'not json', b'[1,2]', b'{"anthropic": {}}', b'"openai-codex"'])
async def test_credentials_bad_format_400(db, body):
    async with client() as c:
        r = await c.post('/api/agent/credentials', content=body, headers={'content-type': 'application/json'})
    assert r.status_code == 400
    assert r.json()['detail'] == '凭据文件格式不正确，请上传 pi 登录后生成的 auth.json'
    assert not os.path.exists(os.path.join(main.settings.AGENT_DATA_DIR, 'auth.json'))


@pytest.mark.asyncio
async def test_credentials_written_atomically_with_0600(db):
    content = {'openai-codex': {'type': 'oauth', 'access': 'very-secret-token', 'refresh': 'r'}}
    async with client() as c:
        r = await c.post('/api/agent/credentials', json=content)
    assert r.status_code == 200 and 'very-secret-token' not in r.text
    path = os.path.join(main.settings.AGENT_DATA_DIR, 'auth.json')
    assert json.load(open(path)) == content
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(main.settings.AGENT_DATA_DIR).st_mode) == 0o700
    assert [n for n in os.listdir(main.settings.AGENT_DATA_DIR) if n != 'auth.json'] == []


@pytest.mark.asyncio
async def test_credentials_size_limit_and_multipart(db):
    big = json.dumps({'openai-codex': {'pad': 'x' * 70_000}}).encode()
    async with client() as c:
        assert (await c.post('/api/agent/credentials', content=big,
                             headers={'content-type': 'application/json'})).status_code == 413
        r = await c.post('/api/agent/credentials', files={'file': ('auth.json', b'{"openai-codex":{}}')})
    assert r.status_code == 415
    assert not os.path.exists(os.path.join(main.settings.AGENT_DATA_DIR, 'auth.json'))


# ---- status / agent / symbols ----

PAIRS = [TokenPair('NVDABUSDT', 'NVDAB', 'NVDA', 1.0), TokenPair('TSLABUSDT', 'TSLAB', 'TSLA', 1.0)]


@pytest.mark.asyncio
async def test_analysis_status_shape(db, monkeypatch):
    async def health():
        return {'status': 'ok', 'auth': 'valid', 'version': 'x'}

    async def pairs(force=False):
        return PAIRS
    monkeypatch.setattr(main.agent_client, 'health', health)
    monkeypatch.setattr(main.short_term.universe, 'get', pairs)
    monkeypatch.setattr(main.alpaca_data, 'api_key', 'k')
    monkeypatch.setattr(main.alpaca_data, 'secret_key', 's')
    monkeypatch.setattr(main.short_term, 'session_limit', 24)
    await put_config(db, ai_picks_push_enabled='1')
    store = AnalysisStore()
    await store.start_session(None, 'NVDA', 'pick')
    async with client() as c:
        s = (await c.get('/api/analysis/status')).json()
    assert s['strategy'] == 'short_term_1w'
    assert s['universe'] == [{'symbol': 'NVDABUSDT', 'underlying_symbol': 'NVDA'},
                             {'symbol': 'TSLABUSDT', 'underlying_symbol': 'TSLA'}]
    assert s['configured']['alpaca'] is True and s['configured']['agent'] is True
    assert set(s['configured']) == {'alpaca', 'binance', 'agent', 'notifications'}
    assert s['agent'] == {'status': 'ok', 'auth': 'valid'}
    assert s['schedule'] == {'times': ['09:00', '21:00'], 'timezone': 'Asia/Singapore', 'enabled': True}
    assert s['sessions_today'] == 1 and s['session_limit'] == 24
    assert s['latest_run'] is None and s['is_running'] is False
    assert 'universe_error' not in s


@pytest.mark.asyncio
async def test_analysis_status_universe_failure(db, monkeypatch):
    async def health():
        return {'status': 'unreachable', 'auth': 'unknown'}

    async def broken(force=False):
        raise MarketDataUnavailable('币安标的池读取失败，且无可用缓存')
    monkeypatch.setattr(main.agent_client, 'health', health)
    monkeypatch.setattr(main.short_term.universe, 'get', broken)
    async with client() as c:
        s = (await c.get('/api/analysis/status')).json()
        assert (await c.get('/api/agent/status')).json() == {'status': 'unreachable', 'auth': 'unknown'}
        sym = (await c.get('/api/bstocks/symbols')).json()
    assert s['universe'] == [] and s['universe_error'] == '币安标的池读取失败，且无可用缓存'
    assert s['configured']['agent'] is False
    assert sym['fallback'] is True
    assert {'symbol': 'NVDABUSDT', 'underlying_symbol': 'NVDA'} in sym['symbols']


@pytest.mark.asyncio
async def test_symbols_from_universe(db, monkeypatch):
    async def pairs(force=False):
        return PAIRS
    monkeypatch.setattr(main.short_term.universe, 'get', pairs)
    async with client() as c:
        sym = (await c.get('/api/bstocks/symbols')).json()
    assert sym == {'symbols': [{'symbol': 'NVDABUSDT', 'underlying_symbol': 'NVDA'},
                               {'symbol': 'TSLABUSDT', 'underlying_symbol': 'TSLA'}]}


# ---- reviews summary ----

@pytest.mark.asyncio
async def test_reviews_summary(db):
    async with aiosqlite.connect(db) as conn:
        rows = [('target', 6.0, 5.0), ('stop', -3.0, -4.0), ('open', 1.5, 0.5), ('target', 4.5, 2.5), (None, 9.0, 9.0)]
        for i, (outcome, ret, excess) in enumerate(rows, 1):
            await conn.execute("INSERT INTO daily_picks(id,symbol,reason) VALUES(?,?,?)", (i, f'S{i}USDT', 'r'))
            await conn.execute('''INSERT INTO analysis_reviews(pick_id,horizon_sessions,as_of,return_pct,
                benchmark_return_pct,excess_return_pct,outcome) VALUES(?,?,?,?,?,?,?)''',
                               (i, 5, '2026-11-10', ret, ret - excess, excess, outcome))
        await conn.commit()
    async with client() as c:
        s = (await c.get('/api/analysis/reviews/summary')).json()
    assert s == {'total': 4, 'target': 2, 'stop': 1, 'open': 1, 'hit_rate_pct': 50.0,
                 'avg_return_pct': 2.25, 'avg_excess_pct': 1.0}


@pytest.mark.asyncio
async def test_reviews_summary_empty(db):
    async with client() as c:
        s = (await c.get('/api/analysis/reviews/summary')).json()
    assert s == {'total': 0, 'target': 0, 'stop': 0, 'open': 0, 'hit_rate_pct': None,
                 'avg_return_pct': None, 'avg_excess_pct': None}


# ---- endpoints delegate to short_term ----

@pytest.mark.asyncio
async def test_analysis_endpoints_use_short_term(db, monkeypatch):
    calls = []

    class Fake:
        store = AnalysisStore()
        is_running = False

        async def run(self, top_n=5, push_notifications=False, run_key=None):
            calls.append(('run', top_n, push_notifications, run_key))
            return {'run_id': 1, 'count': 0, 'picks': [], 'status': 'no_candidates', 'message': ''}

        async def review(self):
            calls.append(('review',))
            return {'count': 0, 'errors': [], 'message': 'm'}

        async def push_run(self, run_id, force=True):
            if run_id == 999:
                raise LookupError('分析记录不存在')
            return {'success': True}

        async def analyze_position(self, symbol, buy_price_usdt=None, quantity=None):
            calls.append(('position', symbol, buy_price_usdt, quantity))
            return {'view': 'hold'}
    monkeypatch.setattr(main, 'short_term', Fake())
    await put_config(db, analysis_top_n='4')
    async with client() as c:
        assert (await c.post('/api/analysis/run?push_notifications=true')).json()['status'] == 'no_candidates'
        assert (await c.post('/api/analysis/review')).json()['message'] == 'm'
        assert (await c.post('/api/analysis/runs/1/push')).json() == {'success': True}
        assert (await c.post('/api/analysis/runs/999/push')).status_code == 404
        pos = await c.post('/api/positions', json={'symbol': 'NVDABUSDT', 'buy_price': 180, 'quantity': 2,
                                                   'include_ai_analysis': False})
        assert (await c.post(f"/api/positions/{pos.json()['id']}/analyze")).json() == {'view': 'hold'}
        assert (await c.post('/api/positions/9999/analyze')).status_code == 404
    assert calls == [('run', 4, True, None), ('review',), ('position', 'NVDABUSDT', 180.0, 2.0)]


def test_position_monitor_uses_short_term_review():
    assert main.position_monitor.trend_provider == main.short_term.analyze_position
    assert not hasattr(main, 'swing_service')


# ---- connection tests ----

@pytest.mark.asyncio
async def test_config_test_alpaca_and_agent(db, monkeypatch):
    seen = {}

    async def bars(self, symbols, days=150):
        seen.update(key=self.api_key, secret=self.secret_key, symbols=symbols)
        return {'SPY': [{'date': '2026-11-13', 'close': 600.0}]}
    monkeypatch.setattr('backend.app.services.short_term.alpaca_data.AlpacaData.get_daily_bars', bars)
    monkeypatch.setattr(main.alpaca_data, 'api_key', 'stored-key')
    monkeypatch.setattr(main.alpaca_data, 'secret_key', 'stored-secret')
    health = {'status': 'ok', 'auth': 'expired'}

    async def fake_health():
        return health
    monkeypatch.setattr(main.agent_client, 'health', fake_health)
    async with client() as c:
        r = (await c.post('/api/config/test', json={'service': 'alpaca', 'api_key': 'AKTE******7890'})).json()
        assert r['success'] is True and seen == {'key': 'stored-key', 'secret': 'stored-secret', 'symbols': ['SPY']}
        r = (await c.post('/api/config/test', json={'service': 'alpaca', 'api_key': 'new-key',
                                                    'api_secret': 'new-secret'})).json()
        assert r['success'] is True and seen['key'] == 'new-key' and seen['secret'] == 'new-secret'
        assert (await c.post('/api/config/test', json={'service': 'agent'})).json()['success'] is False
        health['auth'] = 'valid'
        assert (await c.post('/api/config/test', json={'service': 'agent'})).json()['success'] is True
        assert (await c.post('/api/config/test', json={'service': 'fmp'})).status_code == 400


@pytest.mark.asyncio
async def test_config_test_alpaca_failure(db, monkeypatch):
    async def bars(self, symbols, days=150):
        raise MarketDataUnavailable('Alpaca 行情请求未通过（HTTP 401）')
    monkeypatch.setattr('backend.app.services.short_term.alpaca_data.AlpacaData.get_daily_bars', bars)
    async with client() as c:
        r = (await c.post('/api/config/test', json={'service': 'alpaca', 'api_key': 'k', 'api_secret': 's'})).json()
    assert r == {'success': False, 'message': 'Alpaca 行情请求未通过（HTTP 401）'}


# ---- lifespan ----

@pytest.mark.asyncio
async def test_lifespan_does_not_write_system_configs(db, monkeypatch):
    async def idle():
        return None
    for name in ('background_monitoring_loop', 'background_sync_loop', 'background_analysis_loop',
                 'background_maintenance_loop'):
        monkeypatch.setattr(main, name, idle)

    async def no_close():
        return None
    monkeypatch.setattr(main.position_monitor, 'close', no_close)  # keep the shared monitor usable
    await put_config(db, analysis_top_n='3')
    before = await config_rows(db)
    async with main.lifespan(main.app):
        pass
    assert await config_rows(db) == before == 1
    assert os.path.exists(os.path.join(main.settings.AGENT_DATA_DIR, 'internal_token'))

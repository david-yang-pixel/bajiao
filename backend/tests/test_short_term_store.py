import sqlite3

import aiosqlite
import pytest

from backend.app.database import init_db
from backend.app.services.analysis_store import AnalysisStore


def cols(path, table):
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute(f'PRAGMA table_info({table})')}


@pytest.mark.asyncio
async def test_init_idempotent_and_schema(tmp_path):
    path = str(tmp_path / 't.db')
    await init_db(path)
    await init_db(path)
    assert {'strategy', 'track_status', 'track_updated_at', 'expires_after'} <= cols(path, 'daily_picks')
    assert 'outcome' in cols(path, 'analysis_reviews')
    assert {'pick_id', 'event', 'notified'} <= cols(path, 'pick_events')
    assert {'symbol', 'purpose', 'result_json', 'atr14'} <= cols(path, 'agent_sessions')
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT count(*) FROM system_configs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_legacy_upgrade(tmp_path):
    path = str(tmp_path / 't.db')
    async with aiosqlite.connect(path) as db:
        await db.execute('''CREATE TABLE daily_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
            reason TEXT NOT NULL, target_price REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        await db.execute("INSERT INTO daily_picks (symbol,reason) VALUES ('AAPL','x')")
        await db.commit()
    await init_db(path)
    rows = await AnalysisStore(path).rows('SELECT strategy,track_status FROM daily_picks')
    assert rows == [{'strategy': 'swing_2_4w', 'track_status': 'legacy'}]


@pytest.mark.asyncio
async def test_short_term_picks_and_events(tmp_path):
    path = str(tmp_path / 't.db')
    await init_db(path)
    s = AnalysisStore(path)
    run_id, _ = await s.begin_run('k')
    ids = await s.insert_short_term_picks(run_id, [
        {'symbol': 'AAPL', 'reason': 'r1', 'target_price_usd': 110.0, 'expires_after': '2026-11-23'},
        {'symbol': 'MSFT', 'reason': 'r2', 'target_price_usd': 220.0, 'expires_after': '2026-11-23'}])
    assert len(ids) == 2
    active = await s.active_short_term_picks()
    assert [p['symbol'] for p in active] == ['AAPL', 'MSFT']
    assert active[0]['track_status'] == 'active' and active[0]['run_id'] == run_id
    assert active[0]['expires_after'] == '2026-11-23' and active[0]['id'] == ids[0]
    assert await s.record_pick_event(ids[0], 'target_hit', 110.5, 'ok') is True
    assert await s.record_pick_event(ids[0], 'target_hit', 110.5, 'ok') is False
    assert await s.record_pick_event(ids[1], 'halted', None, 'h') is True
    assert [p['symbol'] for p in await s.active_short_term_picks()] == ['MSFT']
    row = (await s.rows('SELECT track_status,track_updated_at FROM daily_picks WHERE id=?', (ids[0],)))[0]
    assert row['track_status'] == 'target_hit' and row['track_updated_at']
    ev = await s.unnotified_events()
    assert len(ev) == 2
    await s.mark_events_notified([ev[0]['id']])
    assert len(await s.unnotified_events()) == 1


@pytest.mark.asyncio
async def test_sessions(tmp_path):
    path = str(tmp_path / 't.db')
    await init_db(path)
    s = AnalysisStore(path)
    a = await s.start_session(None, 'AAPL', 'screen')
    b = await s.start_session(None, 'AAPL', 'screen')
    c = await s.start_session(None, 'AAPL', 'screen')
    assert await s.sessions_started_since('2000-01-01T00:00:00') == 3
    assert await s.sessions_started_since('2999-01-01T00:00:00') == 0
    await s.finish_session(a, 'ok', 'pick', 100.0, 2.0, {'n': 1}, '')
    await s.finish_session(b, 'ok', 'skip', 101.0, 2.1, {'n': 2}, '')
    await s.finish_session(c, 'failed', None, None, None, {}, 'boom')
    last = await s.last_ok_session('AAPL', 'screen')
    assert last['id'] == b and last['result'] == {'n': 2}
    assert await s.last_ok_session('AAPL', 'review') is None

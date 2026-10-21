import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest

from backend.app import main
from backend.app.database import init_db
from backend.app.services.analysis_errors import AnalysisBusy
from backend.tests.test_swing_analysis import service


@pytest.mark.asyncio
async def test_market_key_is_hidden_preserved_and_reloaded(tmp_path, monkeypatch):
    path = str(tmp_path/'config.db')
    monkeypatch.setattr(main.settings,'DB_PATH',path)
    monkeypatch.setattr(main.settings,'FMP_API_KEY','')
    await init_db(path)
    monkeypatch.setattr(main.feishu_bot_service,'set_credentials',lambda *args:None)
    monkeypatch.setattr(main.feishu_bot_service,'start',lambda:None)
    monkeypatch.setattr(main.feishu_bot_service,'stop',lambda:None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        response=await client.post('/api/config',json={'fmp_api_key':'fmp-secret-test','analysis_top_n':5,
            'analysis_min_score':75,'earnings_blackout_days':5,'ai_picks_push_timezone':'America/New_York'})
        assert response.status_code==200
        cfg=(await client.get('/api/config')).json()
        assert cfg['fmp_api_key']=='' and cfg['has_fmp_key'] is True
        assert 'fmp-secret-test' not in str(cfg)
        assert main.us_market_data.api_key=='fmp-secret-test'
        assert cfg['analysis_top_n']==5 and cfg['analysis_min_score']==75
        assert cfg['ai_picks_push_timezone']=='America/New_York'
        for value in ('','******',None):
            assert (await client.post('/api/config',json={'fmp_api_key':value})).status_code==200
        async with aiosqlite.connect(path) as db:
            assert (await (await db.execute("SELECT value FROM system_configs WHERE key='fmp_api_key'")).fetchone())[0]=='fmp-secret-test'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[
    {'analysis_top_n':2},{'analysis_top_n':6},{'analysis_min_score':101},
    {'earnings_blackout_days':15},{'ai_picks_push_time':'25:30'},
    {'ai_picks_push_timezone':'Not/AZone'},
])
async def test_invalid_configuration_is_atomic_and_does_not_echo_secret(tmp_path,monkeypatch,payload):
    path=str(tmp_path/'invalid.db')
    monkeypatch.setattr(main.settings,'DB_PATH',path)
    await init_db(path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        result=await client.post('/api/config',json={**payload,'fmp_api_key':'do-not-echo'})
        assert result.status_code==422 and 'do-not-echo' not in result.text
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute('SELECT COUNT(*) FROM system_configs')).fetchone())[0]==0


@pytest.mark.asyncio
async def test_optional_password_protects_ui_and_config_not_health(monkeypatch):
    monkeypatch.setattr(main.settings,'ADMIN_PASSWORD','test-console-password')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        assert (await client.get('/')).status_code==401
        assert (await client.get('/api/config')).status_code==401
        assert (await client.get('/api/health')).status_code==200
        assert (await client.get('/',auth=('admin','wrong'))).status_code==401
        assert (await client.get('/',auth=('admin','test-console-password'))).status_code==200


@pytest.mark.asyncio
async def test_http_workflow_preserves_hidden_reports_and_exposes_status(tmp_path,monkeypatch):
    runner,market,agent,notifier,path=await service(tmp_path,monkeypatch)
    monkeypatch.setattr(main,'swing_service',runner)
    monkeypatch.setattr(main,'us_market_data',market)
    monkeypatch.setattr(main,'analysis_agent',agent)
    monkeypatch.setattr(main,'notification_service',notifier)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        status=(await client.get('/api/analysis/status')).json()
        assert len(status['universe'])==8 and len(status['skills'])==3
        assert 'test-fmp' not in str(status)
        response=await client.post('/api/analysis/run')
        assert response.status_code==200 and response.json()['count']==3
        picks=(await client.get('/api/analysis/picks')).json()
        assert len(picks)==3
        assert (await client.delete('/api/analysis/picks')).status_code==200
        assert (await client.get('/api/analysis/picks')).json()==[]
        runs=(await client.get('/api/analysis/runs')).json()
        assert len(runs)==1 and runs[0]['count']==3
        assert (await client.post(f'/api/analysis/runs/{runs[0]["id"]}/push')).json()['success'] is True
        assert (await client.post('/api/analysis/runs/999/push')).status_code==404
    assert len(await runner.store.list_picks(include_hidden=True,all_runs=True))==3


@pytest.mark.asyncio
async def test_concurrent_analysis_does_not_start_second_job(tmp_path,monkeypatch):
    runner,market,agent,notifier,path=await service(tmp_path,monkeypatch)
    started,release=asyncio.Event(),asyncio.Event()
    original=market.get_history.side_effect
    async def blocked(symbol):
        if symbol=='SPY':
            started.set()
            await release.wait()
        return await original(symbol)
    market.get_history.side_effect=blocked
    job=asyncio.create_task(runner.run())
    await started.wait()
    try:
        with pytest.raises(AnalysisBusy):
            await runner.run()
    finally:
        release.set()
        await job
    assert len(await runner.store.list_runs())==1


@pytest.mark.asyncio
async def test_schedule_catches_missed_minute_respects_timezone_and_persists_claim(tmp_path,monkeypatch):
    runner,market,agent,notifier,path=await service(tmp_path,monkeypatch)
    monkeypatch.setattr(main,'swing_service',runner)
    async with aiosqlite.connect(path) as db:
        await db.executemany('INSERT INTO system_configs(key,value) VALUES(?,?)',[
            ('ai_picks_push_enabled','1'),('ai_picks_push_time','09:30'),('ai_picks_push_timezone','America/New_York')])
        await db.commit()
    assert await main.scheduled_analysis_tick(datetime(2026,10,20,12,0,tzinfo=timezone.utc)) is None
    await main.scheduled_analysis_tick(datetime(2026,10,20,14,0,tzinfo=timezone.utc))
    calls=agent.analyze_swing.await_count
    # New runner simulates restart; neither analysis nor successful push is repeated.
    from backend.app.services.swing_analysis import SwingAnalysisService
    monkeypatch.setattr(main,'swing_service',SwingAnalysisService(market,agent,notifier))
    await main.scheduled_analysis_tick(datetime(2026,10,20,15,0,tzinfo=timezone.utc))
    assert agent.analyze_swing.await_count==calls
    assert len(await runner.store.list_runs())==1


@pytest.mark.asyncio
async def test_clear_threshold_and_changed_policy_can_alert_again(tmp_path,monkeypatch):
    from backend.app.services.position_monitor import PositionMonitor
    path=str(tmp_path/'thresholds.db')
    monkeypatch.setattr(main.settings,'DB_PATH',path)
    await init_db(path)
    price=AsyncMock(return_value=95.0)
    send=AsyncMock(return_value=True)
    monkeypatch.setattr(main.binance_client,'get_ticker_price',price)
    monkeypatch.setattr(main.notification_service,'send_position_alert',send)
    monitor=PositionMonitor(binance_client=main.binance_client,notification_service=main.notification_service)
    monkeypatch.setattr(main,'position_monitor',monitor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        created=await client.post('/api/positions',json={'symbol':'NVDABUSDT','buy_price':110,'quantity':1,
            'stop_loss_price':100,'take_profit_price':150,'include_ai_analysis':False})
        pos_id=created.json()['id']
        await monitor.scan_all_positions()
        assert send.await_count==1
        assert (await client.put(f'/api/positions/{pos_id}',json={'stop_loss_price':90,'take_profit_price':None})).status_code==200
        pos=(await monitor.get_active_positions())[0]
        assert pos['take_profit_price'] is None
        price.return_value=85
        await monitor.scan_all_positions()
        assert send.await_count==2
        await client.put(f'/api/positions/{pos_id}',json={'stop_loss_price':90})
        await monitor.scan_all_positions()
        assert send.await_count==2


@pytest.mark.asyncio
async def test_runtime_uses_same_environment_model_as_settings_ui(tmp_path, monkeypatch):
    path = str(tmp_path / 'model-config.db')
    monkeypatch.setattr(main.settings, 'DB_PATH', path)
    monkeypatch.setattr(main.settings, 'OPENAI_MODEL', 'fixture-environment-model')
    await init_db(path)
    monkeypatch.setattr(main.feishu_bot_service, 'set_credentials', lambda *args: None)
    monkeypatch.setattr(main.feishu_bot_service, 'start', lambda: None)
    monkeypatch.setattr(main.feishu_bot_service, 'stop', lambda: None)
    await main.apply_runtime_config()
    assert main.analysis_agent.model == 'fixture-environment-model'
    assert (await main.get_system_config())['openai_model'] == main.analysis_agent.model

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.app import main
from backend.app.database import init_db
from backend.app.services.analysis_errors import AnalysisBusy
from backend.tests.test_swing_analysis import service


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
    from backend.tests.test_notification_preferences import short_term_runner
    runner,notifier,path=await short_term_runner(tmp_path,monkeypatch)
    monkeypatch.setattr(main.agent_client,'health',AsyncMock(return_value={'status':'ok','auth':'valid'}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
        status=(await client.get('/api/analysis/status')).json()
        assert status['strategy']=='short_term_1w' and len(status['universe'])==2
        response=await client.post('/api/analysis/run')
        assert response.status_code==200 and response.json()['count']==2
        picks=(await client.get('/api/analysis/picks')).json()
        assert len(picks)==2
        assert (await client.delete('/api/analysis/picks')).status_code==200
        assert (await client.get('/api/analysis/picks')).json()==[]
        runs=(await client.get('/api/analysis/runs')).json()
        assert len(runs)==1 and runs[0]['count']==2
        assert (await client.post(f'/api/analysis/runs/{runs[0]["id"]}/push')).json()['success'] is True
        assert (await client.post('/api/analysis/runs/999/push')).status_code==404
    assert len(await runner.store.list_picks(include_hidden=True,all_runs=True))==2


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

import asyncio
import base64
import binascii
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import json
import hmac
import math
import logging
import os
import time
from typing import Any, Dict, List
import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from backend.app.config import DEFAULT_CHECK_INTERVAL_HOURS, DEFAULT_INCLUDE_AI_ANALYSIS, settings
from backend.app.database import init_db
from backend.app.models.schemas import (
    PositionCreate,
    PositionOut,
    PositionUpdate,
    AskAIRequest,
    SystemConfigSchema,
    TestConnectionRequest,
    BinanceCredentialsUpdate,
)
from backend.app.services.binance_client import BinanceAccountError, BinanceClient
from backend.app.services.quant_screener import QuantScreener
from backend.app.services.analysis_agent import AnalysisAgent
from backend.app.services.notification import NotificationService
from backend.app.services.position_monitor import PositionMonitor
from backend.app.services.feishu_bot_service import FeishuBotService
from backend.app.services.us_market_data import USMarketData, MarketDataError
from backend.app.services.swing_analysis import SwingAnalysisService, AnalysisUnavailable, AnalysisBusy
from backend.app.services.swing_screener import STOCK_UNIVERSE, HORIZON
from backend.app.services.analysis_skills import skill_metadata

logging.basicConfig(level=logging.INFO)
# HTTPX logs signed request URLs at INFO, including short-lived signatures.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bstocks_app")

# Singletons
binance_client = BinanceClient()
quant_screener = QuantScreener()
analysis_agent = AnalysisAgent()
notification_service = NotificationService()
position_monitor = PositionMonitor(
    binance_client=binance_client,
    notification_service=notification_service,
    quant_screener=quant_screener,
    analysis_agent=analysis_agent
)
feishu_bot_service = FeishuBotService(
    binance_client=binance_client,
    quant_screener=quant_screener,
    analysis_agent=analysis_agent,
    position_monitor=position_monitor
)
us_market_data = USMarketData()
swing_service = SwingAnalysisService(us_market_data, analysis_agent, notification_service)
position_monitor.trend_provider = swing_service.analyze_position

_runtime_config_lock = asyncio.Lock()


async def apply_runtime_config():
    # Concurrent saves must not reload an older snapshot after a newer save.
    async with _runtime_config_lock:
        await _load_runtime_config()


async def _load_runtime_config():
    """Load config from system_configs table and re-initialize service singletons."""
    try:
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT key, value FROM system_configs")
            rows = await cursor.fetchall()
            cfg = {r["key"]: r["value"] for r in rows}

        b_key = cfg.get("binance_api_key") or settings.BINANCE_API_KEY
        b_secret = cfg.get("binance_api_secret") or settings.BINANCE_API_SECRET
        b_mcp = cfg.get("binance_mcp_url") or settings.BINANCE_MCP_URL or "https://agent.binance.com/mcp/agentic"
        oa_key = cfg.get("openai_api_key") or settings.OPENAI_API_KEY
        oa_base = cfg.get("openai_base_url") or settings.OPENAI_BASE_URL or "https://api.openai.com/v1"
        oa_model = cfg.get("openai_model") or settings.OPENAI_MODEL
        feishu_url = cfg.get("feishu_webhook_url") or settings.FEISHU_WEBHOOK_URL
        feishu_app_id = cfg.get("feishu_app_id") or settings.FEISHU_APP_ID
        feishu_app_secret = cfg.get("feishu_app_secret") or settings.FEISHU_APP_SECRET
        wecom_url = cfg.get("wecom_webhook_url") or settings.WECOM_WEBHOOK_URL

        # Update binance client
        binance_client.api_key = b_key
        binance_client.api_secret = b_secret
        binance_client.mcp_url = b_mcp

        # Update analysis agent
        analysis_agent.api_key = oa_key
        analysis_agent.base_url = oa_base
        analysis_agent.model = oa_model
        us_market_data.api_key = cfg.get('fmp_api_key') or settings.FMP_API_KEY
        swing_service._position_cache.clear()

        # Update notification service
        notification_service.feishu_webhook = feishu_url or ""
        notification_service.feishu_webhook_url = feishu_url or ""
        notification_service.wecom_webhook = wecom_url or ""
        notification_service.wecom_webhook_url = wecom_url or ""

        # Update Feishu two-way conversational bot service
        feishu_bot_service.set_credentials(feishu_app_id or "", feishu_app_secret or "")
        if feishu_app_id and feishu_app_secret:
            if not feishu_bot_service.is_connected:
                feishu_bot_service.start()
        else:
            feishu_bot_service.stop()

        logger.info("Runtime configuration successfully reloaded.")
    except Exception as e:
        logger.error(f"Error applying runtime config: {e}")

# Independent workers: slow research and account requests never serialize risk scans.
_monitor_task = None
_background_tasks = []


async def _stored_config():
    return {r['key']:r['value'] for r in await swing_service.store.rows('SELECT key,value FROM system_configs')}


async def _run_swing(push_notifications=False, run_key=None):
    cfg = await _stored_config()
    return await swing_service.run(top_n=int(cfg.get('analysis_top_n',3)),
        min_score=int(cfg.get('analysis_min_score',60)),
        earnings_blackout_days=int(cfg.get('earnings_blackout_days',3)),
        push_notifications=push_notifications, run_key=run_key)


async def scheduled_analysis_tick(now=None):
    cfg = await _stored_config()
    if cfg.get('ai_picks_push_enabled') not in ('1','true','True'):
        return None
    zone = ZoneInfo(cfg.get('ai_picks_push_timezone') or 'Asia/Shanghai')
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    if local.strftime('%H:%M') < (cfg.get('ai_picks_push_time') or '21:30'):
        return None
    # >= catches up after a missed minute; DB run_key survives process restarts.
    return await _run_swing(push_notifications=True,run_key=f'schedule:{local.date().isoformat()}')


async def background_monitoring_loop():
    while True:
        try:
            await position_monitor.scan_all_positions(force_periodic=False)
        except Exception:
            logger.warning('Position scan failed; retrying next tick')
        await asyncio.sleep(30)


async def background_sync_loop():
    while True:
        await asyncio.sleep(600)
        if binance_client.api_key and binance_client.api_secret:
            try:
                await perform_binance_sync(min_notional=1.0)
            except Exception:
                logger.warning('Account sync unavailable; existing holdings preserved')


async def background_analysis_loop():
    while True:
        try:
            await scheduled_analysis_tick()
        except (AnalysisUnavailable, AnalysisBusy):
            pass
        except Exception:
            logger.warning('Scheduled analysis failed; check runtime configuration')
        await asyncio.sleep(30)


async def background_maintenance_loop():
    ticks = 0
    while True:
        await asyncio.sleep(60)
        try:
            await swing_service.retry_pushes()
            ticks += 1
            if ticks % 60 == 0 and us_market_data.api_key:
                await swing_service.review()
        except Exception:
            logger.warning('Delivery/review maintenance unavailable; will retry')


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_task, _background_tasks
    await init_db()
    await apply_runtime_config()
    await swing_service.store.recover_interrupted()
    _background_tasks = [asyncio.create_task(worker()) for worker in (
        background_monitoring_loop, background_sync_loop, background_analysis_loop, background_maintenance_loop)]
    _monitor_task = _background_tasks[0]
    if not settings.ADMIN_PASSWORD:
        logger.warning('Console access password is not configured; restrict access to trusted networks')
    try:
        yield
    finally:
        for task in _background_tasks:
            task.cancel()
        await asyncio.gather(*_background_tasks,return_exceptions=True)
        await position_monitor.close()
        feishu_bot_service.stop()

app = FastAPI(
    title="Binance bStocks Analysis & Alert Bot API",
    version="1.0.0",
    lifespan=lifespan
)
@app.middleware('http')
async def console_access(request: Request, call_next):
    if settings.ADMIN_PASSWORD and request.url.path != '/api/health':
        authorized = False
        try:
            scheme, credentials = request.headers.get('authorization','').split(' ',1)
            user, password = base64.b64decode(credentials,validate=True).decode('utf-8').split(':',1)
            authorized = scheme.lower() == 'basic' and user == 'admin' and hmac.compare_digest(
                password.encode('utf-8'),settings.ADMIN_PASSWORD.encode('utf-8'))
        except (ValueError,UnicodeError,binascii.Error):
            pass
        if not authorized:
            return JSONResponse(status_code=401,content={'detail':'需要控制台访问认证'},
                headers={'WWW-Authenticate':'Basic realm="bStocks", charset="UTF-8"'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(AnalysisUnavailable)
async def analysis_unavailable(request, exc):
    return JSONResponse(status_code=503,content={'detail':str(exc)})


@app.exception_handler(AnalysisBusy)
async def analysis_busy(request, exc):
    return JSONResponse(status_code=409,content={'detail':str(exc)})


@app.exception_handler(MarketDataError)
async def market_unavailable(request, exc):
    return JSONResponse(status_code=503,content={'detail':str(exc)})


@app.exception_handler(RequestValidationError)
async def safe_config_validation_error(request: Request, exc: RequestValidationError):
    errors = [{k:e[k] for k in ('loc','msg','type') if k in e} for e in exc.errors()]
    return JSONResponse(status_code=422,content={'detail':errors})

# Same-origin console; deliberately no wildcard credentialed CORS.

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "binance-bstocks-bot", "revision": os.environ.get('APP_REVISION', 'unknown')}

@app.get("/api/bstocks/symbols")
async def get_symbols():
    symbols = binance_client.get_supported_bstocks()
    return {"symbols": symbols}

@app.get("/api/bstocks/ticker")
async def get_ticker(symbol: str = Query(..., description="e.g. NVDABUSDT")):
    try:
        price = await binance_client.get_ticker_price(symbol)
        return {"symbol": symbol.upper(), "price": price}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/bstocks/klines")
async def get_klines(symbol: str = Query(...), interval: str = "1d", limit: int = 50):
    try:
        data = await binance_client.get_klines(symbol, interval=interval, limit=limit)
        return {"symbol": symbol.upper(), "klines": data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/analysis/run')
async def run_daily_analysis(push_notifications: bool = False):
    return await _run_swing(push_notifications=push_notifications)


@app.get('/api/analysis/status')
async def analysis_status():
    runs = await swing_service.store.list_runs(1)
    running = await swing_service.store.rows("SELECT id FROM analysis_runs WHERE status='running'")
    return {'horizon':HORIZON,'universe':[{'symbol':s,'underlying_symbol':u} for s,u in STOCK_UNIVERSE.items()],
        'skills':skill_metadata(),'configured':{'market_data':bool(us_market_data.api_key),
        'model':bool(analysis_agent.api_key),'notifications':bool(notification_service.feishu_webhook or notification_service.wecom_webhook)},
        'latest_run':runs[0] if runs else None,'is_running':bool(running) or swing_service.is_running}


@app.get('/api/analysis/runs')
async def analysis_runs(limit: int = Query(default=20,ge=1,le=100)):
    return await swing_service.store.list_runs(limit)


@app.post('/api/analysis/runs/{run_id}/push')
async def push_analysis_run(run_id: int):
    try:
        return await swing_service.push_run(run_id)
    except LookupError:
        raise HTTPException(status_code=404,detail='分析记录不存在') from None


@app.post('/api/analysis/review')
async def review_analysis():
    return await swing_service.review()


@app.get('/api/analysis/reviews')
async def analysis_reviews():
    return await swing_service.store.list_reviews()


@app.get('/api/analysis/picks')
async def get_latest_picks():
    return await swing_service.store.list_picks()


@app.delete('/api/analysis/picks')
async def clear_all_picks():
    await swing_service.store.hide_picks()
    return {'message':'已隐藏当前候选，历史报告与复盘仍保留'}


@app.delete('/api/analysis/picks/{pick_id}')
async def delete_single_pick(pick_id: int):
    await swing_service.store.hide_picks(pick_id)
    return {'message':'已隐藏该候选，历史报告仍保留'}


@app.post('/api/positions/{position_id}/analyze')
async def analyze_held_position(position_id: int):
    positions = await swing_service.store.rows('SELECT symbol FROM positions WHERE id=? AND is_active=1',(position_id,))
    if not positions:
        raise HTTPException(status_code=404,detail='未找到正在监控的持仓')
    return await swing_service.analyze_position(positions[0]['symbol'])

@app.get("/api/positions", response_model=List[Dict[str, Any]])
async def list_positions():
    positions = await position_monitor.get_active_positions()
    limit = asyncio.Semaphore(3)
    async def enrich(pos):
        result = dict(pos)
        try:
            async with limit:
                price = float(await binance_client.get_ticker_price(pos['symbol']))
            cost, quantity = float(pos['buy_price']), float(pos['quantity'])
            if not all(math.isfinite(v) and v > 0 for v in (price,cost,quantity)):
                raise ValueError('invalid quote')
            result.update(current_price=price,pnl_pct=round((price/cost-1)*100,2),
                pnl_usd=round((price-cost)*quantity,4),price_status='live')
        except Exception:
            result.update(current_price=None,pnl_pct=None,pnl_usd=None,price_status='unavailable')
        return result
    return await asyncio.gather(*(enrich(pos) for pos in positions))

async def get_default_check_interval_hours(db: aiosqlite.Connection) -> float:
    cursor = await db.execute(
        "SELECT value FROM system_configs WHERE key = 'default_check_interval_hours'"
    )
    row = await cursor.fetchone()
    return float(row[0]) if row else DEFAULT_CHECK_INTERVAL_HOURS


async def get_default_include_ai_analysis(db: aiosqlite.Connection) -> bool:
    cursor = await db.execute(
        "SELECT value FROM system_configs WHERE key = 'default_include_ai_analysis'"
    )
    row = await cursor.fetchone()
    return row[0] == "1" if row else DEFAULT_INCLUDE_AI_ANALYSIS


PREFERENCE_FIELDS = {"check_interval_hours", "ladder_alerts", "include_ai_analysis"}


@app.post("/api/positions")
async def create_position(pos: PositionCreate):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        ladder_json = json.dumps(pos.ladder_alerts)
        interval_hours = pos.check_interval_hours
        if interval_hours is None:
            interval_hours = await get_default_check_interval_hours(db)
        include_ai = pos.include_ai_analysis
        if include_ai is None:
            include_ai = await get_default_include_ai_analysis(db)
        customized = bool(pos.model_fields_set & PREFERENCE_FIELDS)
        cursor = await db.execute(
            """
            INSERT INTO positions (symbol, buy_price, quantity, take_profit_price, stop_loss_price, check_interval_hours, ladder_alerts, include_ai_analysis, preferences_customized, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                pos.symbol.upper(),
                pos.buy_price,
                pos.quantity,
                pos.take_profit_price,
                pos.stop_loss_price,
                interval_hours,
                ladder_json,
                1 if include_ai else 0,
                1 if customized else 0
            )
        )
        await db.commit()
        new_id = cursor.lastrowid
        position_monitor._last_periodic_times[new_id] = time.time()
        return {"id": new_id, "message": "Position created successfully"}

@app.put("/api/positions/{position_id}")
async def update_position(position_id: int, pos: PositionUpdate):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Position not found")

        threshold_changed = any(k in pos.model_fields_set and getattr(pos,k) != row[k]
            for k in ('take_profit_price','stop_loss_price'))
        threshold_changed = threshold_changed or (pos.buy_price is not None and pos.buy_price != row['buy_price'])
        if pos.ladder_alerts is not None:
            try:
                previous_ladder = json.loads(row['ladder_alerts'] or '[]')
            except (ValueError,TypeError):
                previous_ladder = []
            threshold_changed = threshold_changed or pos.ladder_alerts != previous_ladder
        updates = []
        params = []
        if pos.buy_price is not None:
            updates.append("buy_price = ?")
            params.append(pos.buy_price)
        if pos.quantity is not None:
            updates.append("quantity = ?")
            params.append(pos.quantity)
        if 'take_profit_price' in pos.model_fields_set:
            updates.append("take_profit_price = ?")
            params.append(pos.take_profit_price)
        if 'stop_loss_price' in pos.model_fields_set:
            updates.append("stop_loss_price = ?")
            params.append(pos.stop_loss_price)
        if pos.check_interval_hours is not None:
            updates.append("check_interval_hours = ?")
            params.append(pos.check_interval_hours)
        if pos.ladder_alerts is not None:
            updates.append("ladder_alerts = ?")
            params.append(json.dumps(pos.ladder_alerts))
        if pos.include_ai_analysis is not None:
            updates.append("include_ai_analysis = ?")
            params.append(1 if pos.include_ai_analysis else 0)
        if pos.model_fields_set & PREFERENCE_FIELDS:
            updates.append("preferences_customized = 1")

        if updates:
            params.append(position_id)
            await db.execute(f"UPDATE positions SET {', '.join(updates)} WHERE id = ?", params)
            if threshold_changed:
                await db.execute('''UPDATE positions SET alert_reset_after_id=(
                    SELECT COALESCE(MAX(id),0) FROM alert_logs WHERE position_id=?) WHERE id=?''',
                    (position_id,position_id))
            await db.commit()

        if threshold_changed:
            position_monitor.clear_position_milestones(position_id)

    return {"message": "Position updated successfully"}

@app.delete("/api/positions/{position_id}")
async def remove_position(position_id: int):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("UPDATE positions SET is_active = 0 WHERE id = ?", (position_id,))
        await db.commit()
    position_monitor._last_periodic_times.pop(position_id, None)
    position_monitor.clear_position_milestones(position_id)
    return {"message": "Position closed/deactivated"}

@app.post("/api/positions/{position_id}/test-alert")
async def test_position_alert(position_id: int):
    # 1. Verify webhook configuration
    if not notification_service.feishu_webhook and not notification_service.wecom_webhook:
        raise HTTPException(
            status_code=400,
            detail="尚未配置飞书或企业微信 Webhook 机器人地址，请前往【系统配置】页面填写并保存后再试。"
        )

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="持仓标的不存在")
        pos = dict(row)

    # 2. Get latest ticker price
    buy_price = float(pos.get("buy_price") or 0.0)
    try:
        curr_price = await binance_client.get_ticker_price(pos["symbol"])
    except Exception:
        raise HTTPException(status_code=503,detail='币安行情暂不可用，未用买入成本冒充现价') from None

    pnl_pct = ((curr_price - buy_price) / buy_price) * 100.0 if buy_price > 0 else 0.0

    interval_hours = float(pos.get("check_interval_hours") if pos.get("check_interval_hours") is not None else DEFAULT_CHECK_INTERVAL_HOURS)
    if interval_hours <= 0:
        interval_str = "已关闭定时播报"
    elif interval_hours < 1:
        interval_str = f"每 {int(round(interval_hours * 60))} 分钟"
    elif interval_hours == int(interval_hours):
        interval_str = f"每 {int(interval_hours)} 小时"
    else:
        interval_str = f"每 {interval_hours:.1f} 小时"

    note = f"手动点击测试（当前设定周期: {interval_str}）"

    # Risk/test notification never waits for the separate AI supplement.
    trend_analysis = None

    sent = await notification_service.send_position_alert(
        symbol=pos["symbol"],
        buy_price=buy_price,
        curr_price=curr_price,
        pnl_pct=pnl_pct,
        alert_type="periodic",
        extra_note=note,
        trend_analysis=trend_analysis
    )

    if not sent:
        await position_monitor.log_alert(pos['id'],pos['symbol'],note,'periodic',status='failed')
        raise HTTPException(
            status_code=502,
            detail="飞书/企微机器人发送失败，请检查系统配置中的 Webhook 地址是否有效或机器人是否已被群移除。"
        )

    # Record to alert logs
    await position_monitor.log_alert(pos["id"], pos["symbol"], note, "periodic")
    position_monitor._last_periodic_times[pos["id"]] = time.time()
    if pos.get('include_ai_analysis'):
        position_monitor._schedule_supplement(pos['id'],pos['symbol'],buy_price,curr_price,note)

    return {
        "success": True,
        "message": f"已推送 {pos['symbol']} 持仓提醒；AI 分析（如已配置）单独补充",
        "triggered": ["periodic"],
        "curr_price": round(curr_price, 2),
        "pnl_pct": round(pnl_pct, 2)
    }

async def perform_binance_sync(min_notional: float = 1.0) -> Dict[str, Any]:
    """
    Sync user's real Binance Spot & Funding holdings into active positions monitor:
    - Queries both Spot account (GET /api/v3/account) and Funding account (POST /sapi/v1/asset/get-funding-asset).
    - Filters dust assets with total notional value < min_notional (default 1.0 USDT).
    - Calculates accurate weighted average buy price from Spot trades or Convert flow.
    - Preserves user-adjusted buy price for existing positions if no new trade history is available.
    - Automatically deactivates closed positions that no longer exist or are below dust threshold.
    """
    if not binance_client.api_key or not binance_client.api_secret:
        return {
            "success": False,
            "count": 0,
            "synced": [],
            "deactivated": [],
            "message": "未配置币安 API Key 或 API Secret"
        }

    balances = await binance_client.get_account_balances()
    synced_items = []
    valid_symbols = set()

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        default_interval = await get_default_check_interval_hours(db)
        default_include_ai = await get_default_include_ai_analysis(db)
        cursor = await db.execute("SELECT symbol FROM positions WHERE is_active = 1")
        active_symbols = {row["symbol"] for row in await cursor.fetchall()}

        for b in balances:
            asset = b["asset"].upper()
            total_qty = float(b["total"])

            # Ignore common settlement coins and zero balances
            if asset in ["USDT", "BUSD", "FDUSD", "USDC", "BNB", "LDUSDT", "DAI"]:
                continue
            if total_qty <= 1e-6:
                continue

            # bStocks symbols matching (e.g. AMZNB -> AMZNBUSDT; AAPL -> AAPLBUSDT)
            candidates = []
            if asset.endswith("USDT"):
                candidates.append(asset)
            else:
                candidates.append(f"{asset}USDT")
                if not asset.endswith("B"):
                    candidates.append(f"{asset}BUSDT")

            matched_symbol = None
            curr_price = 0.0

            for sym in candidates:
                try:
                    price = await binance_client.get_ticker_price(sym)
                    if price > 0:
                        matched_symbol = sym
                        curr_price = price
                        break
                except Exception:
                    continue

            if not matched_symbol:
                if active_symbols.intersection(candidates):
                    raise RuntimeError("币安行情读取失败，本次同步已取消，现有持仓保持不变")
                continue

            notional = total_qty * curr_price
            if notional < min_notional:
                logger.info(
                    f"忽略小额粉尘资产 {asset}: 数量 {total_qty}, 当前市值 ${notional:.4f} < ${min_notional}"
                )
                continue

            valid_symbols.add(matched_symbol)

            # Calculate accurate average buy price, fallback to current price
            avg_buy = await binance_client.get_average_buy_price(matched_symbol)
            buy_price = avg_buy if (avg_buy and avg_buy > 0) else curr_price

            # New holdings use the saved reporting default; reimported symbols retain preferences.
            tp_price = round(buy_price * 1.05, 4)
            sl_price = round(buy_price * 0.97, 4)
            default_ladder = [2.0, 3.0, 5.0, 8.0]

            cursor = await db.execute(
                "SELECT id, buy_price FROM positions WHERE symbol = ? AND is_active = 1",
                (matched_symbol,)
            )
            existing = await cursor.fetchone()

            if existing:
                # If avg_buy is not found from trade history, preserve user-set buy_price
                final_buy_price = avg_buy if (avg_buy and avg_buy > 0) else (existing["buy_price"] if existing["buy_price"] and existing["buy_price"] > 0 else curr_price)
                await db.execute(
                    """
                    UPDATE positions
                    SET quantity = ?, buy_price = ?
                    WHERE id = ?
                    """,
                    (total_qty, final_buy_price, existing["id"])
                )
                synced_items.append({
                    "symbol": matched_symbol,
                    "action": "updated",
                    "quantity": total_qty,
                    "buy_price": final_buy_price,
                    "current_price": curr_price,
                    "notional": round(notional, 2),
                    "wallet": b.get("wallet", "币安账户")
                })
            else:
                # Only settings the user chose carry over; legacy rows hold old hardcoded defaults.
                cursor = await db.execute(
                    """
                    SELECT check_interval_hours, ladder_alerts, include_ai_analysis
                    FROM positions WHERE symbol = ? AND is_active = 0 AND preferences_customized = 1
                    ORDER BY id DESC LIMIT 1
                    """,
                    (matched_symbol,)
                )
                previous = await cursor.fetchone()
                interval_hours = default_interval
                ladder_json = json.dumps(default_ladder)
                include_ai = int(default_include_ai)
                if previous:
                    if previous["check_interval_hours"] is not None:
                        interval_hours = previous["check_interval_hours"]
                    if previous["ladder_alerts"] is not None:
                        ladder_json = previous["ladder_alerts"]
                    include_ai = int(bool(previous["include_ai_analysis"]))
                await db.execute(
                    """
                    INSERT INTO positions (symbol, buy_price, quantity, take_profit_price, stop_loss_price, check_interval_hours, ladder_alerts, include_ai_analysis, preferences_customized, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        matched_symbol,
                        buy_price,
                        total_qty,
                        tp_price,
                        sl_price,
                        interval_hours,
                        ladder_json,
                        include_ai,
                        1 if previous else 0
                    )
                )
                synced_items.append({
                    "symbol": matched_symbol,
                    "action": "added",
                    "quantity": total_qty,
                    "buy_price": buy_price,
                    "current_price": curr_price,
                    "notional": round(notional, 2),
                    "wallet": b.get("wallet", "币安账户")
                })

        # Automatically deactivate active positions that have been cleared on Binance
        cursor = await db.execute("SELECT id, symbol FROM positions WHERE is_active = 1")
        active_rows = await cursor.fetchall()
        deactivated = []
        for r in active_rows:
            if r["symbol"] not in valid_symbols:
                await db.execute("UPDATE positions SET is_active = 0 WHERE id = ?", (r["id"],))
                deactivated.append(r["symbol"])

        await db.commit()

    msg = f"成功同步 {len(synced_items)} 个币安有效持仓"
    if deactivated:
        msg += f"，已自动平仓移除 {len(deactivated)} 个已卖出或粉尘标的 ({', '.join(deactivated)})"

    return {
        "success": True,
        "count": len(synced_items),
        "synced": synced_items,
        "deactivated": deactivated,
        "message": msg
    }

@app.post("/api/positions/sync-binance")
async def sync_binance_positions():
    """
    Sync user's real Binance Spot holdings into active positions monitor.
    Uses Binance signed /api/v3/account to get non-zero balances.
    """
    if not binance_client.api_key or not binance_client.api_secret:
        raise HTTPException(
            status_code=400,
            detail="需要同时在「系统与 API 配置」中配置币安 API Key 和 API Secret 才能同步持仓"
        )

    try:
        res = await perform_binance_sync(min_notional=1.0)
        return res
    except Exception as e:
        logger.error(f"Failed to fetch Binance account balances: {e}")
        raise HTTPException(status_code=500, detail=f"同步币安持仓失败: {str(e)}")

@app.get("/api/alerts/logs")
async def get_alert_logs():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM alert_logs ORDER BY id DESC LIMIT 50")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

# ==========================================
# System Configuration & Connectivity APIs
# ==========================================

@app.get("/api/config")
async def get_system_config():
    """Retrieve system settings from DB with fallback to environment variables."""
    cfg = {}
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT key, value FROM system_configs")
        rows = await cursor.fetchall()
        for r in rows:
            cfg[r["key"]] = r["value"]

    def mask(val: str) -> str:
        if not val or len(val) <= 8:
            return "******" if val else ""
        return val[:4] + "******" + val[-4:]

    raw_binance_key = cfg.get("binance_api_key") or settings.BINANCE_API_KEY
    raw_binance_secret = cfg.get("binance_api_secret") or settings.BINANCE_API_SECRET
    raw_openai_key = cfg.get("openai_api_key") or settings.OPENAI_API_KEY
    raw_feishu_secret = cfg.get("feishu_app_secret") or settings.FEISHU_APP_SECRET
    raw_fmp_key = cfg.get('fmp_api_key') or settings.FMP_API_KEY

    return {
        "binance_api_key": mask(raw_binance_key),
        "binance_api_secret": "",
        "binance_mcp_url": cfg.get("binance_mcp_url") or settings.BINANCE_MCP_URL or "https://agent.binance.com/mcp/agentic",
        "has_binance_key": bool(raw_binance_key),
        "has_binance_secret": bool(raw_binance_secret),
        "openai_api_key": mask(raw_openai_key),
        "openai_base_url": cfg.get("openai_base_url") or settings.OPENAI_BASE_URL or "https://api.openai.com/v1",
        "openai_model": cfg.get("openai_model") or settings.OPENAI_MODEL,
        "has_openai_key": bool(raw_openai_key),
        "feishu_webhook_url": cfg.get("feishu_webhook_url") or settings.FEISHU_WEBHOOK_URL or "",
        "feishu_app_id": cfg.get("feishu_app_id") or settings.FEISHU_APP_ID or "",
        "feishu_app_secret": mask(raw_feishu_secret),
        "has_feishu_app_secret": bool(raw_feishu_secret),
        "feishu_bot_connected": feishu_bot_service.is_connected,
        "wecom_webhook_url": cfg.get("wecom_webhook_url") or settings.WECOM_WEBHOOK_URL or "",
        "ai_picks_push_enabled": cfg.get("ai_picks_push_enabled") in ("1", "true", "True", True),
        "ai_picks_push_time": cfg.get("ai_picks_push_time") or "21:30",
        "default_check_interval_hours": float(cfg.get("default_check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS)),
        "default_include_ai_analysis": cfg["default_include_ai_analysis"] == "1" if "default_include_ai_analysis" in cfg else DEFAULT_INCLUDE_AI_ANALYSIS,
        'fmp_api_key':'','has_fmp_key':bool(raw_fmp_key),
        'analysis_horizon':HORIZON,'analysis_top_n':int(cfg.get('analysis_top_n',3)),
        'analysis_min_score':int(cfg.get('analysis_min_score',60)),
        'earnings_blackout_days':int(cfg.get('earnings_blackout_days',3)),
        'ai_picks_push_timezone':cfg.get('ai_picks_push_timezone') or 'Asia/Shanghai'
    }

@app.post("/api/config")
async def save_system_config(payload: SystemConfigSchema):
    """Save updated system settings and reload runtime services."""
    dumped = payload.model_dump(exclude_unset=True)
    fields_to_update = {}

    # Old tabs may submit masks; real credentials must use the verified pair endpoint.
    for key in ("binance_api_key", "binance_api_secret"):
        value = str(dumped.get(key) or "").strip()
        if value and "******" not in value:
            raise HTTPException(status_code=400, detail="普通设置保存不会修改币安凭据，请使用“验证并更新币安凭据”操作")

    for k in ["binance_mcp_url", "openai_base_url", "openai_model", "feishu_webhook_url", "wecom_webhook_url", "ai_picks_push_time", "feishu_app_id", "default_check_interval_hours", 'analysis_top_n','analysis_min_score','earnings_blackout_days','ai_picks_push_timezone']:
        if k in dumped and dumped[k] is not None:
            val = str(dumped[k]).strip()
            if val:
                fields_to_update[k] = val

    for flag_k in ["ai_picks_push_enabled", "default_include_ai_analysis"]:
        if flag_k in dumped and dumped[flag_k] is not None:
            fields_to_update[flag_k] = "1" if dumped[flag_k] else "0"

    # Only overwrite keys if provided and not masked
    for secret_k in ["openai_api_key", "feishu_app_secret"]:
        if secret_k in dumped and dumped[secret_k] and "******" not in str(dumped[secret_k]):
            fields_to_update[secret_k] = str(dumped[secret_k]).strip()
    if payload.fmp_api_key is not None:
        value = payload.fmp_api_key.get_secret_value().strip()
        if value and '*' not in value:
            fields_to_update['fmp_api_key'] = value

    async with aiosqlite.connect(settings.DB_PATH) as db:
        for k, v in fields_to_update.items():
            if v is not None:
                await db.execute(
                    "INSERT INTO system_configs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (k, str(v))
                )
        await db.commit()

    await apply_runtime_config()
    return {"message": "配置已成功保存并实时生效"}


@app.post("/api/config/binance-credentials")
async def update_binance_credentials(payload: BinanceCredentialsUpdate, request: Request):
    """Replace a complete pair only after both signed account reads succeed."""
    api_key = payload.api_key.get_secret_value()
    api_secret = payload.api_secret.get_secret_value()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        stored_pair = dict(await (await db.execute(
            "SELECT key,value FROM system_configs WHERE key IN ('binance_api_key','binance_api_secret')"
        )).fetchall())
    current_secret = stored_pair.get("binance_api_secret") or settings.BINANCE_API_SECRET
    proof = payload.current_api_secret.get_secret_value() if payload.current_api_secret else ""
    local_setup = request.client and request.client.host in ("127.0.0.1", "::1") and not any(
        header in request.headers for header in ("forwarded", "x-forwarded-for", "x-real-ip")
    )
    if not local_setup and (not current_secret or not hmac.compare_digest(proof.encode("utf-8"), current_secret.encode("utf-8"))):
        raise HTTPException(status_code=403, detail="更换币安凭据需验证当前 API Secret，原有凭据保持不变")
    candidate = BinanceClient(api_key=api_key, api_secret=api_secret, mcp_url="")
    try:
        await candidate.get_account_balances()
    except BinanceAccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"币安账户验证未通过（{type(exc).__name__}），原有凭据保持不变") from None

    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        latest_pair = dict(await (await db.execute(
            "SELECT key,value FROM system_configs WHERE key IN ('binance_api_key','binance_api_secret')"
        )).fetchall())
        if latest_pair != stored_pair:
            raise HTTPException(status_code=409, detail="币安凭据在验证期间发生变化，请刷新后重试")
        await db.executemany(
            "INSERT INTO system_configs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            [("binance_api_key", api_key), ("binance_api_secret", api_secret)],
        )
        await db.commit()
    await apply_runtime_config()
    return {"success": True, "message": "币安现货和资金账户验证成功，凭据已安全更新并生效"}

@app.post("/api/config/test")
async def test_connection(req: TestConnectionRequest):
    """Test connectivity for Binance, LLM, Feishu, or WeCom."""
    svc = req.service.lower()
    if svc == 'fmp':
        key = req.api_key
        if not key or '*' in key:
            cfg = await _stored_config()
            key = cfg.get('fmp_api_key') or settings.FMP_API_KEY
        provider = USMarketData(api_key=key or '')
        try:
            history = await provider.get_history('NVDA')
            research = await provider.get_research('NVDA')
            if len(history) < 252 or not research['financials']:
                return {'success':False,'message':'FMP 已连接，但完整波段分析所需历史或财务数据不足'}
            return {'success':True,'message':'FMP 日线、季度财务、新闻和财报日历读取成功'}
        except MarketDataError as exc:
            return {'success':False,'message':str(exc)}
    if svc == "binance":
        api_key = req.api_key
        if not api_key or "******" in api_key:
            api_key = binance_client.api_key
        api_secret = req.api_secret.get_secret_value() if req.api_secret else ""
        if not api_secret or "******" in api_secret:
            api_secret = binance_client.api_secret
        if not api_key or not api_secret:
            return {"success": False, "message": "请先配置完整的币安 API Key 和 API Secret"}
        candidate = BinanceClient(api_key=api_key, api_secret=api_secret, mcp_url="")
        try:
            await candidate.get_account_balances()
            return {"success": True, "message": "币安现货和资金账户鉴权成功，持仓读取正常"}
        except BinanceAccountError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            return {"success": False, "message": f"币安账户读取未通过（{type(exc).__name__}）"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        if svc == "llm":
            base_url = (req.base_url or analysis_agent.base_url or settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            api_key = req.api_key
            if not api_key or "******" in api_key:
                api_key = analysis_agent.api_key or settings.OPENAI_API_KEY
            if not api_key:
                return {"success": False, "message": "未提供有效的大模型 API Key"}

            model = req.model or analysis_agent.model or "gemini-2.5-flash"
            try:
                res = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 5
                    }
                )
                if res.status_code == 200:
                    return {"success": True, "message": f"大模型接口连通成功 ({model})"}
                else:
                    return {"success": False, "message": f"模型调用失败（HTTP {res.status_code}），请检查配置与额度"}
            except Exception as e:
                return {"success": False, "message": "大模型接口网络或响应异常"}

        elif svc == "feishu":
            url = req.webhook_url or notification_service.feishu_webhook_url
            if not url:
                return {"success": False, "message": "未配置飞书 Webhook 地址"}
            try:
                res = await client.post(
                    url,
                    json={
                        "msg_type": "text",
                        "content": {"text": "【Binance bStocks 机器人】系统设置连通性测试消息：发送成功！"}
                    }
                )
                data = res.json()
                if res.status_code == 200 and (data.get('code') == 0 or data.get('StatusCode') == 0):
                    return {"success": True, "message": "飞书测试消息已发送"}
                else:
                    return {"success": False, "message": "飞书返回非成功状态，请检查 Webhook"}
            except Exception as e:
                return {"success": False, "message": "飞书连接异常，请检查 Webhook"}

        elif svc == "wecom":
            url = req.webhook_url or notification_service.wecom_webhook_url
            if not url:
                return {"success": False, "message": "未配置企业微信 Webhook 地址"}
            try:
                res = await client.post(
                    url,
                    json={
                        "msg_type": "markdown",
                        "markdown": {"content": "### 🔔 Binance bStocks 机器人\n> **测试消息**：企业微信连通性测试成功！"}
                    }
                )
                data = res.json()
                if data.get("errcode") == 0:
                    return {"success": True, "message": "企业微信测试消息已发送"}
                else:
                    return {"success": False, "message": "企业微信返回非成功状态，请检查 Webhook"}
            except Exception as e:
                return {"success": False, "message": "企业微信连接异常，请检查 Webhook"}

        elif svc in ["feishu_bot", "feishu_app"]:
            async with aiosqlite.connect(settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT key, value FROM system_configs WHERE key IN ('feishu_app_id', 'feishu_app_secret')")
                rows = await cursor.fetchall()
                cfg_dict = {r["key"]: r["value"] for r in rows}

            app_id = req.app_id or cfg_dict.get("feishu_app_id") or settings.FEISHU_APP_ID
            app_secret = req.app_secret or cfg_dict.get("feishu_app_secret") or settings.FEISHU_APP_SECRET
            if not app_id or not app_secret or "******" in app_secret:
                app_secret = cfg_dict.get("feishu_app_secret") or settings.FEISHU_APP_SECRET

            if not app_id or not app_secret:
                return {"success": False, "message": "未配置飞书 App ID 或 App Secret"}

            try:
                res = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret}
                )
                data = res.json()
                if data.get("code") == 0:
                    status_text = "长连接正常运行" if feishu_bot_service.is_connected else "鉴权通过（长连接已就绪）"
                    return {"success": True, "message": f"飞书自建应用机器人鉴权成功！当前状态: {status_text}"}
                else:
                    return {"success": False, "message": "飞书应用鉴权失败，请检查 App ID/Secret"}
            except Exception as e:
                return {"success": False, "message": "飞书应用连接异常"}

        else:
            raise HTTPException(status_code=400, detail="不支持的服务测试类型")

@app.post("/api/feishu/events")
async def feishu_events_callback(request: Request, background_tasks: BackgroundTasks):
    """
    HTTP Callback for Feishu Event Subscriptions (Supports URL Verification & Incoming Messages).
    """
    try:
        data = await request.json()
    except Exception:
        return {"code": 0}

    # 1. URL Challenge verification
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    # 2. Event handling
    header = data.get("header", {})
    event_type = header.get("event_type")

    if event_type == "im.message.receive_v1":
        event = data.get("event", {})
        msg = event.get("message", {})
        content_str = msg.get("content", "{}")
        try:
            content_dict = json.loads(content_str)
            text = content_dict.get("text", "")
        except Exception:
            text = ""
        msg_id = msg.get("message_id")
        chat_id = msg.get("chat_id")
        sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")

        if text and msg_id:
            background_tasks.add_task(
                feishu_bot_service.handle_incoming_text,
                text, msg_id, chat_id, sender_id
            )

    return {"code": 0}

# Static Files mount for Web Dashboard
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

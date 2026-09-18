import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from typing import Any, Dict, List
import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.models.schemas import (
    PositionCreate,
    PositionOut,
    PositionUpdate,
    AskAIRequest,
    SystemConfigSchema,
    TestConnectionRequest,
)
from backend.app.services.binance_client import BinanceClient
from backend.app.services.quant_screener import QuantScreener
from backend.app.services.analysis_agent import AnalysisAgent
from backend.app.services.notification import NotificationService
from backend.app.services.position_monitor import PositionMonitor
from backend.app.services.feishu_bot_service import FeishuBotService

logging.basicConfig(level=logging.INFO)
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

async def apply_runtime_config():
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
        oa_model = cfg.get("openai_model") or "gpt-4o-mini"
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

# Background worker loop
_monitor_task = None
_last_ai_picks_push_date = ""

async def background_monitoring_loop():
    global _last_ai_picks_push_date
    logger.info("Background position monitoring loop started (30s interval).")
    tick_count = 0
    tz_bj = timezone(timedelta(hours=8))
    while True:
        try:
            await asyncio.sleep(30)
            tick_count += 1
            force_periodic = (tick_count % 120 == 0)
            await position_monitor.scan_all_positions(force_periodic=force_periodic)

            # Every 10 mins (20 ticks), silently sync Binance positions if API keys are configured
            if tick_count % 20 == 0 and binance_client.api_key and binance_client.api_secret:
                try:
                    await perform_binance_sync(min_notional=1.0)
                except Exception as sync_err:
                    logger.warning(f"Periodic binance sync warning: {sync_err}")

            # Check daily scheduled AI stock picks push (Beijing Time UTC+8)
            try:
                now_bj = datetime.now(tz_bj)
                current_hm = now_bj.strftime("%H:%M")
                today_date = now_bj.strftime("%Y-%m-%d")

                async with aiosqlite.connect(settings.DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT key, value FROM system_configs WHERE key IN ('ai_picks_push_enabled', 'ai_picks_push_time')"
                    )
                    rows = await cursor.fetchall()
                    sched_cfg = {r["key"]: r["value"] for r in rows}
                    is_sched_enabled = sched_cfg.get("ai_picks_push_enabled") in ("1", "true", "True", True)
                    sched_time = sched_cfg.get("ai_picks_push_time") or "21:30"

                    if is_sched_enabled and current_hm == sched_time and _last_ai_picks_push_date != today_date:
                        _last_ai_picks_push_date = today_date
                        logger.info(f"Triggering scheduled daily AI stock picks push for {today_date} at {current_hm} (Beijing Time)...")
                        await run_daily_analysis()
            except Exception as sched_err:
                logger.error(f"Error checking scheduled AI picks push: {sched_err}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background monitor tick: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _monitor_task
    logger.info("Initializing database...")
    await init_db()
    await apply_runtime_config()
    _monitor_task = asyncio.create_task(background_monitoring_loop())
    yield
    if _monitor_task:
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    feishu_bot_service.stop()
    logger.info("App shutdown complete.")

app = FastAPI(
    title="Binance bStocks Analysis & Alert Bot API",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "binance-bstocks-bot"}

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

@app.post("/api/analysis/run")
async def run_daily_analysis():
    candidates = await quant_screener.screen_candidates(binance_client, min_score=40)
    top_candidates = candidates[:3] if candidates else []

    results = []
    async with aiosqlite.connect(settings.DB_PATH) as db:
        # Clear previous picks so the newly generated run completely refreshes the recommendations
        await db.execute("DELETE FROM daily_picks")
        await db.commit()

        for cand in top_candidates:
            symbol = cand["symbol"]
            analysis = await analysis_agent.analyze_candidate(
                symbol=symbol,
                score=cand["score"],
                signals=cand["signals"],
                metrics=cand["metrics"]
            )
            analysis["score"] = cand["score"]
            results.append(analysis)

            await db.execute(
                """
                INSERT INTO daily_picks (symbol, reason, target_price)
                VALUES (?, ?, ?)
                """,
                (symbol, analysis["reason"], analysis["target_price"])
            )
            await db.commit()

            await notification_service.send_daily_pick_card(
                symbol=symbol,
                reason=analysis["reason"],
                target_price=analysis["target_price"],
                stop_loss=analysis["stop_loss_price"],
                score=cand["score"]
            )

    return {"count": len(results), "picks": results}

@app.get("/api/analysis/picks")
async def get_latest_picks():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM daily_picks ORDER BY id DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

@app.delete("/api/analysis/picks")
async def clear_all_picks():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM daily_picks")
        await db.commit()
    return {"message": "All picks cleared successfully"}

@app.delete("/api/analysis/picks/{pick_id}")
async def delete_single_pick(pick_id: int):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM daily_picks WHERE id = ?", (pick_id,))
        await db.commit()
    return {"message": "Pick deleted successfully"}

@app.get("/api/positions", response_model=List[Dict[str, Any]])
async def list_positions():
    positions = await position_monitor.get_active_positions()
    enriched = []
    for pos in positions:
        curr_price = None
        pnl_pct = 0.0
        try:
            curr_price = await binance_client.get_ticker_price(pos["symbol"])
            buy_price = float(pos["buy_price"])
            pnl_pct = ((curr_price - buy_price) / buy_price) * 100.0
        except Exception:
            pass

        pos_dict = dict(pos)
        pos_dict["current_price"] = curr_price
        pos_dict["pnl_pct"] = round(pnl_pct, 2)
        enriched.append(pos_dict)
    return enriched

@app.post("/api/positions")
async def create_position(pos: PositionCreate):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        ladder_json = json.dumps(pos.ladder_alerts)
        cursor = await db.execute(
            """
            INSERT INTO positions (symbol, buy_price, quantity, take_profit_price, stop_loss_price, check_interval_hours, ladder_alerts, include_ai_analysis, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                pos.symbol.upper(),
                pos.buy_price,
                pos.quantity,
                pos.take_profit_price,
                pos.stop_loss_price,
                pos.check_interval_hours,
                ladder_json,
                1 if pos.include_ai_analysis else 0
            )
        )
        await db.commit()
        return {"id": cursor.lastrowid, "message": "Position created successfully"}

@app.put("/api/positions/{position_id}")
async def update_position(position_id: int, pos: PositionUpdate):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM positions WHERE id = ?", (position_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Position not found")

        updates = []
        params = []
        if pos.buy_price is not None:
            updates.append("buy_price = ?")
            params.append(pos.buy_price)
        if pos.quantity is not None:
            updates.append("quantity = ?")
            params.append(pos.quantity)
        if pos.take_profit_price is not None:
            updates.append("take_profit_price = ?")
            params.append(pos.take_profit_price)
        if pos.stop_loss_price is not None:
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

        if updates:
            params.append(position_id)
            await db.execute(f"UPDATE positions SET {', '.join(updates)} WHERE id = ?", params)
            await db.commit()

    return {"message": "Position updated successfully"}

@app.delete("/api/positions/{position_id}")
async def remove_position(position_id: int):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("UPDATE positions SET is_active = 0 WHERE id = ?", (position_id,))
        await db.commit()
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
        curr_price = buy_price

    pnl_pct = ((curr_price - buy_price) / buy_price) * 100.0 if buy_price > 0 else 0.0

    interval_hours = float(pos.get("check_interval_hours") if pos.get("check_interval_hours") is not None else 0.5)
    if interval_hours <= 0:
        interval_str = "已关闭定时播报"
    elif interval_hours < 1:
        interval_str = f"每 {int(round(interval_hours * 60))} 分钟"
    elif interval_hours == int(interval_hours):
        interval_str = f"每 {int(interval_hours)} 小时"
    else:
        interval_str = f"每 {interval_hours:.1f} 小时"

    note = f"手动点击测试（当前设定周期: {interval_str}）"

    trend_analysis = None
    if bool(pos.get("include_ai_analysis")):
        trend_analysis = await position_monitor.get_symbol_trend_analysis(pos["symbol"])

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
        raise HTTPException(
            status_code=502,
            detail="飞书/企微机器人发送失败，请检查系统配置中的 Webhook 地址是否有效或机器人是否已被群移除。"
        )

    # Record to alert logs
    await position_monitor.log_alert(pos["id"], pos["symbol"], note, "periodic")

    return {
        "success": True,
        "message": f"已成功向飞书推送 {pos['symbol']} 实时持仓卡片！最新价: ${curr_price:.2f}, 盈亏: {pnl_pct:+.2f}%",
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

            # Default short-term take profit +5%, tight stop loss -3%, high-frequency 30m check
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
                await db.execute(
                    """
                    INSERT INTO positions (symbol, buy_price, quantity, take_profit_price, stop_loss_price, check_interval_hours, ladder_alerts, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        matched_symbol,
                        buy_price,
                        total_qty,
                        tp_price,
                        sl_price,
                        0.5,
                        json.dumps(default_ladder)
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

    return {
        "binance_api_key": mask(raw_binance_key),
        "binance_api_secret": mask(raw_binance_secret),
        "binance_mcp_url": cfg.get("binance_mcp_url") or settings.BINANCE_MCP_URL or "https://agent.binance.com/mcp/agentic",
        "has_binance_key": bool(raw_binance_key),
        "has_binance_secret": bool(raw_binance_secret),
        "openai_api_key": mask(raw_openai_key),
        "openai_base_url": cfg.get("openai_base_url") or settings.OPENAI_BASE_URL or "https://api.openai.com/v1",
        "openai_model": cfg.get("openai_model") or "gpt-4o-mini",
        "has_openai_key": bool(raw_openai_key),
        "feishu_webhook_url": cfg.get("feishu_webhook_url") or settings.FEISHU_WEBHOOK_URL or "",
        "feishu_app_id": cfg.get("feishu_app_id") or settings.FEISHU_APP_ID or "",
        "feishu_app_secret": mask(raw_feishu_secret),
        "has_feishu_app_secret": bool(raw_feishu_secret),
        "feishu_bot_connected": feishu_bot_service.is_connected,
        "wecom_webhook_url": cfg.get("wecom_webhook_url") or settings.WECOM_WEBHOOK_URL or "",
        "ai_picks_push_enabled": cfg.get("ai_picks_push_enabled") in ("1", "true", "True", True),
        "ai_picks_push_time": cfg.get("ai_picks_push_time") or "21:30"
    }

@app.post("/api/config")
async def save_system_config(payload: SystemConfigSchema):
    """Save updated system settings and reload runtime services."""
    dumped = payload.model_dump(exclude_unset=True)
    fields_to_update = {}

    for k in ["binance_mcp_url", "openai_base_url", "openai_model", "feishu_webhook_url", "wecom_webhook_url", "ai_picks_push_time", "feishu_app_id"]:
        if k in dumped and dumped[k] is not None:
            val = str(dumped[k]).strip()
            if val:
                fields_to_update[k] = val

    if "ai_picks_push_enabled" in dumped and dumped["ai_picks_push_enabled"] is not None:
        fields_to_update["ai_picks_push_enabled"] = "1" if dumped["ai_picks_push_enabled"] else "0"

    # Only overwrite keys if provided and not masked
    for secret_k in ["binance_api_key", "binance_api_secret", "openai_api_key", "feishu_app_secret"]:
        if secret_k in dumped and dumped[secret_k] and "******" not in str(dumped[secret_k]):
            fields_to_update[secret_k] = str(dumped[secret_k]).strip()

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

@app.post("/api/config/test")
async def test_connection(req: TestConnectionRequest):
    """Test connectivity for Binance, LLM, Feishu, or WeCom."""
    svc = req.service.lower()
    async with httpx.AsyncClient(timeout=10.0) as client:
        if svc == "binance":
            api_key = req.api_key
            if not api_key or "******" in api_key:
                api_key = binance_client.api_key or settings.BINANCE_API_KEY

            # 1. First test Binance Official REST API connectivity
            rest_url = "https://api.binance.com/api/v3/ping"
            headers = {}
            if api_key and "******" not in api_key:
                headers["X-MBX-APIKEY"] = api_key

            rest_ok = False
            rest_msg = ""
            try:
                r_rest = await client.get(rest_url, headers=headers)
                if r_rest.status_code == 200:
                    rest_ok = True
                    rest_msg = "币安官方行情 API 连通正常 (HTTP 200)"
                else:
                    rest_msg = f"REST 返回: {r_rest.status_code}"
            except Exception as e:
                rest_msg = f"REST 连接异常: {str(e)}"

            # 2. Test Binance MCP endpoint if configured
            mcp_url = req.base_url or binance_client.mcp_url or "https://agent.binance.com/mcp/agentic"
            mcp_msg = ""
            mcp_ok = False
            try:
                # Test MCP JSON-RPC ping / initialize via POST
                rpc_headers = {"Content-Type": "application/json"}
                if api_key and "******" not in api_key:
                    rpc_headers["X-MBX-APIKEY"] = api_key
                r_mcp = await client.post(
                    mcp_url,
                    headers=rpc_headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
                    timeout=5.0
                )
                if r_mcp.status_code in (200, 204):
                    mcp_ok = True
                    mcp_msg = "币安 MCP Agentic 服务响应正常"
                elif r_mcp.status_code == 401:
                    mcp_msg = "币安 MCP 需要特定权限认证 (返回 401)"
                else:
                    mcp_msg = f"MCP 状态码: {r_mcp.status_code}"
            except Exception as e:
                mcp_msg = f"MCP 连接异常: {str(e)}"

            if rest_ok:
                return {
                    "success": True,
                    "message": f"币安连通测试成功！{rest_msg}。系统已具备稳定获取 bStocks 行情能力。" + (f" (MCP状态: {mcp_msg})" if mcp_msg else "")
                }
            else:
                return {
                    "success": False,
                    "message": f"币安 API 连接受阻: {rest_msg} / {mcp_msg}"
                }

        elif svc == "llm":
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
                    return {"success": False, "message": f"模型调用失败: HTTP {res.status_code} - {res.text[:200]}"}
            except Exception as e:
                return {"success": False, "message": f"大模型接口请求异常: {str(e)}"}

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
                if data.get("code") == 0 or res.status_code == 200:
                    return {"success": True, "message": "飞书测试消息已发送"}
                else:
                    return {"success": False, "message": f"飞书返回错误: {res.text}"}
            except Exception as e:
                return {"success": False, "message": f"飞书连接异常: {str(e)}"}

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
                    return {"success": False, "message": f"企业微信返回错误: {res.text}"}
            except Exception as e:
                return {"success": False, "message": f"企业微信连接异常: {str(e)}"}

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
                    return {"success": False, "message": f"飞书应用鉴权失败: {data.get('msg')} (code: {data.get('code')})"}
            except Exception as e:
                return {"success": False, "message": f"飞书应用连接异常: {str(e)}"}

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

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set
import aiosqlite

from backend.app.config import DEFAULT_CHECK_INTERVAL_HOURS, settings
from backend.app.services.binance_client import BinanceClient
from backend.app.services.notification import NotificationService
from backend.app.services.quant_screener import QuantScreener
from backend.app.services.analysis_agent import AnalysisAgent

logger = logging.getLogger(__name__)

class PositionMonitor:
    """
    Periodic position monitor and composite alerting engine:
    1. Periodic inspection (check_interval_hours)
    2. Real-time threshold alerts (Take-profit, Stop-loss, Ladder percentages)
    3. Anti-spam / alert cooldown mechanisms
    4. Optional AI future trend analysis for position alerts
    """

    def __init__(
        self,
        binance_client: Optional[BinanceClient] = None,
        notification_service: Optional[NotificationService] = None,
        quant_screener: Optional[QuantScreener] = None,
        analysis_agent: Optional[AnalysisAgent] = None,
        db_path: Optional[str] = None
    ):
        self.binance_client = binance_client or BinanceClient()
        self.notification_service = notification_service or NotificationService()
        self.quant_screener = quant_screener
        self.analysis_agent = analysis_agent
        self._db_path = db_path
        self._last_alert_times: Dict[str, float] = {}  # {f"{pos_id}:{alert_type}:{step}": timestamp}
        self._last_periodic_times: Dict[int, float] = {}  # {pos_id: timestamp}
        self._achieved_milestones: Set[str] = set()  # {f"{pos_id}:{alert_type}:{step}"}
        self.cooldown_seconds = 1800  # 30 mins anti-spam cooldown per step

    @property
    def db_path(self) -> str:
        return self._db_path or settings.DB_PATH

    @db_path.setter
    def db_path(self, val: Optional[str]):
        self._db_path = val

    async def get_active_positions(self) -> List[Dict[str, Any]]:
        """Fetch all active positions from the database."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE is_active = 1"
            )
            rows = await cursor.fetchall()
            positions = []
            for row in rows:
                p = dict(row)
                if isinstance(p.get("ladder_alerts"), str):
                    try:
                        p["ladder_alerts"] = json.loads(p["ladder_alerts"])
                    except Exception:
                        p["ladder_alerts"] = []
                positions.append(p)
            return positions

    async def log_alert(self, position_id: int, symbol: str, message: str, alert_type: str):
        """Record alert into alert_logs table."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO alert_logs (position_id, symbol, message, alert_type)
                VALUES (?, ?, ?, ?)
                """,
                (position_id, symbol, message, alert_type)
            )
            await db.commit()

    async def get_last_periodic_alert_time(self, pos_id: int) -> Optional[float]:
        """Fetch the timestamp of the last periodic alert for a position from alert_logs."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT created_at FROM alert_logs WHERE position_id = ? AND alert_type = 'periodic' ORDER BY id DESC LIMIT 1",
                    (pos_id,)
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    return dt.timestamp()
        except Exception as e:
            logger.warning(f"Error querying last periodic alert time for pos {pos_id}: {e}")
        return None

    async def is_milestone_achieved(
        self,
        pos_id: int,
        key: str,
        alert_type: str,
        message_pattern: Optional[str] = None
    ) -> bool:
        """Check if a threshold or ladder milestone has already been alerted for this position."""
        if key in self._achieved_milestones:
            return True
        try:
            async with aiosqlite.connect(self.db_path) as db:
                if message_pattern:
                    cursor = await db.execute(
                        "SELECT 1 FROM alert_logs WHERE position_id = ? AND alert_type = ? AND message LIKE ? LIMIT 1",
                        (pos_id, alert_type, message_pattern)
                    )
                else:
                    cursor = await db.execute(
                        "SELECT 1 FROM alert_logs WHERE position_id = ? AND alert_type = ? LIMIT 1",
                        (pos_id, alert_type)
                    )
                row = await cursor.fetchone()
                if row:
                    self._achieved_milestones.add(key)
                    return True
        except Exception as e:
            logger.warning(f"Error checking milestone achievement for {key}: {e}")
        return False

    def mark_milestone_achieved(self, key: str):
        self._achieved_milestones.add(key)

    def reset_milestone(self, key: str):
        self._achieved_milestones.discard(key)

    def clear_position_milestones(self, pos_id: int):
        prefix = f"{pos_id}:"
        self._achieved_milestones = {k for k in self._achieved_milestones if not k.startswith(prefix)}

    def _is_cooled_down(self, key: str, custom_cooldown: Optional[float] = None) -> bool:
        last_time = self._last_alert_times.get(key, 0)
        cd = custom_cooldown if custom_cooldown is not None else self.cooldown_seconds
        return (time.time() - last_time) > cd

    def _mark_alerted(self, key: str):
        self._last_alert_times[key] = time.time()

    async def check_single_position(self, pos: Dict[str, Any], force_periodic: bool = False) -> List[str]:
        """
        Evaluate price for one position and dispatch alerts if thresholds are breached.
        """
        triggered = []
        pos_id = pos["id"]
        symbol = pos["symbol"]
        buy_price = float(pos["buy_price"])
        qty = float(pos.get("quantity") or 0.0)

        try:
            curr_price = await self.binance_client.get_ticker_price(symbol)
        except Exception as e:
            logger.error(f"Failed to fetch ticker price for {symbol}: {e}")
            return triggered

        pnl_pct = ((curr_price - buy_price) / buy_price) * 100.0
        pnl_usd = (curr_price - buy_price) * qty if qty > 0 else None

        include_ai = bool(pos.get("include_ai_analysis"))
        trend_cache = None

        async def get_trend():
            nonlocal trend_cache
            if not include_ai:
                return None
            if trend_cache is None:
                trend_cache = await self.get_symbol_trend_analysis(symbol)
            return trend_cache

        # 1. Take Profit alert
        tp_price = pos.get("take_profit_price")
        if tp_price and curr_price >= float(tp_price):
            key = f"{pos_id}:take_profit"
            if not await self.is_milestone_achieved(pos_id, key, "take_profit"):
                note = f"已突破设定止盈位 ${float(tp_price):.2f}"
                trend_data = await get_trend()
                await self.notification_service.send_position_alert(
                    symbol, buy_price, curr_price, pnl_pct, "take_profit", note, trend_analysis=trend_data, pnl_usd=pnl_usd
                )
                await self.log_alert(pos_id, symbol, note, "take_profit")
                self.mark_milestone_achieved(key)
                self._mark_alerted(key)
                triggered.append("take_profit")
        elif tp_price and curr_price < float(tp_price) * 0.98:
            self.reset_milestone(f"{pos_id}:take_profit")

        # 2. Stop Loss alert
        sl_price = pos.get("stop_loss_price")
        if sl_price and curr_price <= float(sl_price):
            key = f"{pos_id}:stop_loss"
            if not await self.is_milestone_achieved(pos_id, key, "stop_loss"):
                note = f"已跌破设定止损线 ${float(sl_price):.2f}"
                trend_data = await get_trend()
                await self.notification_service.send_position_alert(
                    symbol, buy_price, curr_price, pnl_pct, "stop_loss", note, trend_analysis=trend_data, pnl_usd=pnl_usd
                )
                await self.log_alert(pos_id, symbol, note, "stop_loss")
                self.mark_milestone_achieved(key)
                self._mark_alerted(key)
                triggered.append("stop_loss")
        elif sl_price and curr_price > float(sl_price) * 1.02:
            self.reset_milestone(f"{pos_id}:stop_loss")

        # 3. Ladder profit percentage alerts (e.g. 2.0, 3.0, 5.0, 8.0)
        ladder_alerts = sorted([float(s) for s in (pos.get("ladder_alerts") or [])])
        for step_val in ladder_alerts:
            if pnl_pct >= step_val:
                key = f"{pos_id}:ladder:{step_val:.1f}"
                if not await self.is_milestone_achieved(pos_id, key, "ladder", f"%+{step_val:.1f}%"):
                    note = f"收益率达成阶梯目标 +{step_val:.1f}%"
                    await self.notification_service.send_position_alert(
                        symbol, buy_price, curr_price, pnl_pct, "ladder", note, pnl_usd=pnl_usd
                    )
                    await self.log_alert(pos_id, symbol, note, "ladder")
                    self.mark_milestone_achieved(key)
                    self._mark_alerted(key)
                    triggered.append(f"ladder_{step_val}")

        # 4. Periodic inspection notification (configured frequency e.g. every 15m, 30m, 1h, 2h, 4h)
        interval_hours = float(pos.get("check_interval_hours") if pos.get("check_interval_hours") is not None else DEFAULT_CHECK_INTERVAL_HOURS)
        is_periodic_due = False

        if interval_hours > 0:
            interval_sec = interval_hours * 3600
            now = time.time()
            last_p = self._last_periodic_times.get(pos_id)
            if last_p is None:
                last_p = await self.get_last_periodic_alert_time(pos_id)
                if last_p is None:
                    last_p = now
                self._last_periodic_times[pos_id] = last_p

            if (now - last_p) >= interval_sec:
                is_periodic_due = True

        if force_periodic or is_periodic_due:
            # Update last periodic time immediately so duplicate alerts cannot occur
            self._last_periodic_times[pos_id] = time.time()

            if interval_hours <= 0:
                interval_str = "手动触发"
            elif interval_hours < 1:
                interval_str = f"每 {int(round(interval_hours * 60))} 分钟"
            elif interval_hours == int(interval_hours):
                interval_str = f"每 {int(interval_hours)} 小时"
            else:
                interval_str = f"每 {interval_hours:.1f} 小时"

            note = f"定时巡检汇报（周期: {interval_str}）"
            trend_data = await get_trend()

            await self.notification_service.send_position_alert(
                symbol, buy_price, curr_price, pnl_pct, "periodic", note, trend_analysis=trend_data, pnl_usd=pnl_usd
            )
            await self.log_alert(pos_id, symbol, note, "periodic")
            triggered.append("periodic")

        return triggered

    async def get_symbol_trend_analysis(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch klines and generate short-term AI trend analysis for position alert."""
        if not self.quant_screener or not self.analysis_agent:
            return None
        try:
            klines = await self.binance_client.get_klines(symbol, interval="1d", limit=40)
            if not klines or len(klines) < 5:
                klines = await self.binance_client.get_klines(symbol, interval="1h", limit=40)
            if not klines:
                return None
            df = self.quant_screener.calculate_indicators(klines)
            scored = self.quant_screener.score_stock(df)
            analysis = await self.analysis_agent.analyze_candidate(
                symbol=symbol,
                score=scored.get("score", 0),
                signals=scored.get("signals", []),
                metrics=scored.get("metrics", {})
            )
            return analysis
        except Exception as e:
            logger.warning(f"Error generating AI trend analysis for {symbol}: {e}")
            return None

    async def scan_all_positions(self, force_periodic: bool = False):
        """Scan all active positions."""
        positions = await self.get_active_positions()
        for pos in positions:
            await self.check_single_position(pos, force_periodic=force_periodic)

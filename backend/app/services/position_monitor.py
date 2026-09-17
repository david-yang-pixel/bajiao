import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional
import aiosqlite

from backend.app.config import settings
from backend.app.services.binance_client import BinanceClient
from backend.app.services.notification import NotificationService

logger = logging.getLogger(__name__)

class PositionMonitor:
    """
    Periodic position monitor and composite alerting engine:
    1. Periodic inspection (check_interval_hours)
    2. Real-time threshold alerts (Take-profit, Stop-loss, Ladder percentages)
    3. Anti-spam / alert cooldown mechanisms
    """

    def __init__(
        self,
        binance_client: Optional[BinanceClient] = None,
        notification_service: Optional[NotificationService] = None,
        db_path: Optional[str] = None
    ):
        self.binance_client = binance_client or BinanceClient()
        self.notification_service = notification_service or NotificationService()
        self._db_path = db_path
        self._last_alert_times: Dict[str, float] = {}  # {f"{pos_id}:{alert_type}:{step}": timestamp}
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

    def _is_cooled_down(self, key: str) -> bool:
        last_time = self._last_alert_times.get(key, 0)
        return (time.time() - last_time) > self.cooldown_seconds

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

        try:
            curr_price = await self.binance_client.get_ticker_price(symbol)
        except Exception as e:
            logger.error(f"Failed to fetch ticker price for {symbol}: {e}")
            return triggered

        pnl_pct = ((curr_price - buy_price) / buy_price) * 100.0

        # 1. Take Profit alert
        tp_price = pos.get("take_profit_price")
        if tp_price and curr_price >= float(tp_price):
            key = f"{pos_id}:take_profit"
            if self._is_cooled_down(key):
                note = f"已突破设定止盈位 ${tp_price:.2f}"
                await self.notification_service.send_position_alert(
                    symbol, buy_price, curr_price, pnl_pct, "take_profit", note
                )
                await self.log_alert(pos_id, symbol, note, "take_profit")
                self._mark_alerted(key)
                triggered.append("take_profit")

        # 2. Stop Loss alert
        sl_price = pos.get("stop_loss_price")
        if sl_price and curr_price <= float(sl_price):
            key = f"{pos_id}:stop_loss"
            if self._is_cooled_down(key):
                note = f"已跌破设定止损线 ${sl_price:.2f}"
                await self.notification_service.send_position_alert(
                    symbol, buy_price, curr_price, pnl_pct, "stop_loss", note
                )
                await self.log_alert(pos_id, symbol, note, "stop_loss")
                self._mark_alerted(key)
                triggered.append("stop_loss")

        # 3. Ladder profit percentage alerts (e.g. 5.0, 10.0, 20.0)
        ladder_alerts = pos.get("ladder_alerts") or []
        for step in ladder_alerts:
            step_val = float(step)
            if pnl_pct >= step_val:
                key = f"{pos_id}:ladder:{step_val}"
                if self._is_cooled_down(key):
                    note = f"收益率达成阶梯目标 +{step_val:.1f}%"
                    await self.notification_service.send_position_alert(
                        symbol, buy_price, curr_price, pnl_pct, "ladder", note
                    )
                    await self.log_alert(pos_id, symbol, note, "ladder")
                    self._mark_alerted(key)
                    triggered.append(f"ladder_{step_val}")

        # 4. Force periodic inspection notification
        if force_periodic:
            note = f"定时常规巡检（周期: 每{pos.get('check_interval_hours', 1)}小时）"
            await self.notification_service.send_position_alert(
                symbol, buy_price, curr_price, pnl_pct, "periodic", note
            )
            await self.log_alert(pos_id, symbol, note, "periodic")
            triggered.append("periodic")

        return triggered

    async def scan_all_positions(self, force_periodic: bool = False):
        """Scan all active positions."""
        positions = await self.get_active_positions()
        for pos in positions:
            await self.check_single_position(pos, force_periodic=force_periodic)

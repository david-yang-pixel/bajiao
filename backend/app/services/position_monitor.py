import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
import aiosqlite

from backend.app.config import DEFAULT_CHECK_INTERVAL_HOURS, settings
from backend.app.services.binance_client import BinanceClient, position_market
from backend.app.services.notification import NotificationService
from backend.app.services.quant_screener import QuantScreener
from backend.app.services.analysis_agent import AnalysisAgent

logger = logging.getLogger(__name__)


class PositionMonitor:
    """Price-risk alerts first; optional 2–4 week AI supplements run separately."""

    def __init__(
        self,
        binance_client: Optional[BinanceClient] = None,
        notification_service: Optional[NotificationService] = None,
        quant_screener: Optional[QuantScreener] = None,
        analysis_agent: Optional[AnalysisAgent] = None,
        db_path: Optional[str] = None,
        trend_provider: Optional[Callable[[str], Awaitable[Optional[Dict[str, Any]]]]] = None,
    ):
        self.binance_client = binance_client or BinanceClient()
        self.notification_service = notification_service or NotificationService()
        self.quant_screener = quant_screener
        self.analysis_agent = analysis_agent
        self.trend_provider = trend_provider
        self._db_path = db_path
        self._last_alert_times: Dict[str, float] = {}
        self._last_periodic_times: Dict[int, float] = {}
        # An initial inspection baseline is not a successfully delivered alert.
        self._periodic_started_times: Dict[int, float] = {}
        self._achieved_milestones: Set[str] = set()
        self._position_locks: Dict[int, asyncio.Lock] = {}
        self._supplement_tasks: Dict[int, asyncio.Task] = {}
        self._closed = False
        self.cooldown_seconds = 1800

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
            cursor = await db.execute("SELECT * FROM positions WHERE is_active = 1")
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

    async def log_alert(
        self, position_id: int, symbol: str, message: str, alert_type: str,
        status: str = "success",
    ):
        """Record delivery outcome; failed attempts never count as milestones."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO alert_logs (position_id, symbol, message, alert_type, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (position_id, symbol, message, alert_type, status),
            )
            await db.commit()

    async def get_last_periodic_alert_time(self, pos_id: int) -> Optional[float]:
        """Fetch only the last successfully delivered periodic alert."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT created_at FROM alert_logs WHERE position_id = ? "
                    "AND alert_type = 'periodic' AND status = 'success' ORDER BY id DESC LIMIT 1",
                    (pos_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    return dt.timestamp()
        except Exception:
            logger.warning("Unable to query periodic alert time for pos %s", pos_id)
        return None

    async def is_milestone_achieved(
        self,
        pos_id: int,
        key: str,
        alert_type: str,
        message_pattern: Optional[str] = None,
    ) -> bool:
        """Check successfully delivered threshold/ladder milestones."""
        if key in self._achieved_milestones:
            return True
        try:
            async with aiosqlite.connect(self.db_path) as db:
                if message_pattern:
                    cursor = await db.execute(
                        "SELECT 1 FROM alert_logs WHERE position_id = ? AND alert_type = ? "
                        "AND status = 'success' AND id > COALESCE((SELECT alert_reset_after_id FROM positions WHERE id=alert_logs.position_id),0) AND message LIKE ? LIMIT 1",
                        (pos_id, alert_type, message_pattern),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT 1 FROM alert_logs WHERE position_id = ? AND alert_type = ? "
                        "AND status = 'success' AND id > COALESCE((SELECT alert_reset_after_id FROM positions WHERE id=alert_logs.position_id),0) LIMIT 1",
                        (pos_id, alert_type),
                    )
                row = await cursor.fetchone()
                if row:
                    self._achieved_milestones.add(key)
                    return True
        except Exception:
            logger.warning("Unable to check milestone for pos %s", pos_id)
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

    @staticmethod
    def _finite_float(value: Any) -> Optional[float]:
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (TypeError, ValueError, OverflowError):
            return None

    async def check_single_position(self, pos: Dict[str, Any], force_periodic: bool = False) -> List[str]:
        """Serialize checks per position, without ever awaiting AI analysis."""
        pos_id = pos["id"]
        lock = self._position_locks.setdefault(pos_id, asyncio.Lock())
        previous_periodic = self._last_periodic_times.get(pos_id)
        async with lock:
            # Coalesce concurrent forced inspections if one was just delivered.
            force_periodic = force_periodic and self._last_periodic_times.get(pos_id) == previous_periodic
            try:
                return await self._check_single_position(pos, force_periodic)
            except Exception:
                # Never include remote exception text (webhook URLs may contain secrets).
                logger.warning("Position check failed for pos %s", pos_id)
                return []

    async def _check_single_position(self, pos: Dict[str, Any], force_periodic: bool) -> List[str]:
        triggered = []
        notes = []
        pos_id = pos["id"]
        symbol = pos["symbol"]
        if not pos.get("is_active", True):
            return triggered
        buy_price = self._finite_float(pos.get("buy_price"))
        if buy_price is None or buy_price <= 0:
            logger.warning("Invalid cost price for pos %s", pos_id)
            return triggered
        quote_currency = position_market(symbol)['quote_currency']
        if (pos.get('cost_currency') or quote_currency) != quote_currency:
            return triggered  # No implicit USD/stablecoin conversion in return alerts.
        qty = self._finite_float(pos.get("quantity")) or 0.0
        try:
            curr_price = self._finite_float(await self.binance_client.get_ticker_price(symbol))
        except Exception:
            logger.warning("Unable to fetch ticker price for pos %s", pos_id)
            return triggered
        if curr_price is None or curr_price <= 0:
            logger.warning("Invalid current price for pos %s", pos_id)
            return triggered

        pnl_pct = ((curr_price - buy_price) / buy_price) * 100.0
        if not math.isfinite(pnl_pct):
            logger.warning("Invalid price return for pos %s", pos_id)
            return triggered
        pnl_usd = (curr_price - buy_price) * qty if qty > 0 else None
        if pnl_usd is not None and not math.isfinite(pnl_usd):
            pnl_usd = None

        async def deliver(alert_type, note, key=None, label=None):
            try:
                success = bool(await self.notification_service.send_position_alert(
                    symbol, buy_price, curr_price, pnl_pct, alert_type, note,
                    pnl_usd=pnl_usd,
                ))
            except Exception:
                success = False
            try:
                await self.log_alert(pos_id, symbol, note, alert_type,
                                     status="success" if success else "failed")
            except Exception:
                logger.warning("Unable to record alert outcome for pos %s", pos_id)
            if not success:
                logger.warning("Position alert delivery failed for pos %s (%s)", pos_id, alert_type)
                return
            if key:
                self.mark_milestone_achieved(key)
                self._mark_alerted(key)
            if alert_type == "periodic":
                self._last_periodic_times[pos_id] = time.time()
            triggered.append(label or alert_type)
            notes.append(note)

        # Threshold reset semantics intentionally remain unchanged.
        tp_price = self._finite_float(pos.get("take_profit_price"))
        if tp_price is not None and tp_price > 0:
            key = f"{pos_id}:take_profit"
            if curr_price >= tp_price:
                if not await self.is_milestone_achieved(pos_id, key, "take_profit"):
                    await deliver("take_profit", f"已突破设定止盈位 ${tp_price:.2f}", key)
            elif curr_price < tp_price * 0.98:
                self.reset_milestone(key)

        sl_price = self._finite_float(pos.get("stop_loss_price"))
        if sl_price is not None and sl_price > 0:
            key = f"{pos_id}:stop_loss"
            if curr_price <= sl_price:
                if not await self.is_milestone_achieved(pos_id, key, "stop_loss"):
                    await deliver("stop_loss", f"已跌破设定止损线 ${sl_price:.2f}", key)
            elif curr_price > sl_price * 1.02:
                self.reset_milestone(key)

        # Ladder values remain percentage points, e.g. 2.0 means +2%, not +200%.
        ladder_alerts = sorted({step for value in (pos.get("ladder_alerts") or [])
                                if (step := self._finite_float(value)) is not None and step > 0})
        for step_val in ladder_alerts:
            if pnl_pct >= step_val:
                key = f"{pos_id}:ladder:{step_val:.1f}"
                if not await self.is_milestone_achieved(pos_id, key, "ladder", f"%+{step_val:.1f}%"):
                    await deliver("ladder", f"收益率达成阶梯目标 +{step_val:.1f}%",
                                  key, f"ladder_{step_val}")

        raw_interval = pos.get("check_interval_hours")
        interval_hours = self._finite_float(DEFAULT_CHECK_INTERVAL_HOURS if raw_interval is None else raw_interval) or 0.0
        is_periodic_due = False
        if interval_hours > 0:
            now = time.time()
            last_p = self._last_periodic_times.get(pos_id)
            if last_p is None:
                last_p = await self.get_last_periodic_alert_time(pos_id)
                if last_p is not None:
                    self._last_periodic_times[pos_id] = last_p
                else:
                    last_p = self._periodic_started_times.setdefault(pos_id, now)
            is_periodic_due = (now - last_p) >= interval_hours * 3600

        if force_periodic or is_periodic_due:
            if interval_hours <= 0:
                interval_str = "手动触发"
            elif interval_hours < 1:
                interval_str = f"每 {int(round(interval_hours * 60))} 分钟"
            elif interval_hours == int(interval_hours):
                interval_str = f"每 {int(interval_hours)} 小时"
            else:
                interval_str = f"每 {interval_hours:.1f} 小时"
            await deliver("periodic", f"定时巡检汇报（周期: {interval_str}）")

        # Schedule only after risk notifications; no AI work runs in this check.
        if triggered and pos.get("include_ai_analysis"):
            self._schedule_supplement(pos_id, symbol, buy_price, curr_price, "; ".join(notes))
        return triggered

    def _schedule_supplement(self, pos_id: int, symbol: str, buy_price: float,
                             curr_price: float, note: str):
        existing = self._supplement_tasks.get(pos_id)
        if self._closed or (existing is not None and not existing.done()):
            return
        task = asyncio.create_task(self._send_supplement(pos_id, symbol, buy_price, curr_price, note))
        self._supplement_tasks[pos_id] = task

        def done(completed):
            if self._supplement_tasks.get(pos_id) is completed:
                self._supplement_tasks.pop(pos_id, None)

        task.add_done_callback(done)

    async def _send_supplement(self, pos_id: int, symbol: str, buy_price: float,
                               curr_price: float, note: str):
        try:
            analysis = await self.get_symbol_trend_analysis(symbol)
            if not analysis or self._closed:
                return
            content = (
                f"AI 波段分析补充 | Binance 标的: {symbol}\n"
                f"持有周期: 2–4 周 (2–4 weeks)\n"
                f"提醒原因: {note}\n"
                f"价格基准 (price basis): Binance 代币报价；买入成本 ${buy_price:.2f}；"
                f"风险提醒时现价 ${curr_price:.2f}\n"
                f"分析价格基准: {analysis.get('price_basis') or 'Binance 代币报价（非正股报价）'}\n"
                f"分析理由 (reason): {analysis.get('reason') or '暂无'}\n"
                f"研判目标价: {analysis.get('target_price', '--')} | "
                f"防守止损价: {analysis.get('stop_loss_price', '--')}\n"
                "补充分析不改变已发送的风险提醒。"
            )
            # Re-read authoritative state after AI completes, not the scan snapshot.
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT 1 FROM positions WHERE id = ? AND symbol = ? AND is_active = 1",
                    (pos_id, symbol),
                )
                active = await cursor.fetchone()
            if active and not self._closed:
                if not await self.notification_service.send_text(content):
                    logger.warning("Analysis supplement delivery failed for pos %s", pos_id)
        except Exception:
            logger.warning("Analysis supplement failed for pos %s", pos_id)

    async def close(self):
        """Stop accepting supplements and cancel/join every outstanding AI task."""
        self._closed = True
        tasks = list(self._supplement_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._supplement_tasks.clear()

    async def get_symbol_trend_analysis(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Use the swing provider when wired, retaining the legacy manual API."""
        if self.trend_provider is not None:
            return await self.trend_provider(symbol)
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
            return await self.analysis_agent.analyze_candidate(
                symbol=symbol,
                score=scored.get("score", 0),
                signals=scored.get("signals", []),
                metrics=scored.get("metrics", {}),
            )
        except Exception:
            logger.warning("Unable to generate AI trend analysis")
            return None

    async def scan_all_positions(self, force_periodic: bool = False):
        """Scan active positions; one broken position cannot abort the others."""
        positions = await self.get_active_positions()
        for pos in positions:
            try:
                await self.check_single_position(pos, force_periodic=force_periodic)
            except Exception:
                logger.warning("Unable to scan position %s", pos.get("id"))

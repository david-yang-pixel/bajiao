import logging
from typing import Any, Dict, Optional
import httpx

from backend.app.config import settings

logger = logging.getLogger(__name__)

class NotificationService:
    """
    Sends rich alert & daily analysis cards to Feishu (Lark) and WeChat Work (WeCom).
    """

    def __init__(
        self,
        feishu_webhook: Optional[str] = None,
        wecom_webhook: Optional[str] = None,
        timeout: float = 8.0
    ):
        self.feishu_webhook = feishu_webhook or settings.FEISHU_WEBHOOK_URL
        self.wecom_webhook = wecom_webhook or getattr(settings, "WECOM_WEBHOOK_URL", "")
        self.timeout = timeout

    async def send_text(self, content: str) -> bool:
        """
        Send plain text notification to configured webhooks.
        """
        success = False
        if self.feishu_webhook:
            payload = {
                "msg_type": "text",
                "content": {"text": content}
            }
            res = await self._post(self.feishu_webhook, payload)
            success = success or res

        if self.wecom_webhook:
            payload = {
                "msg_type": "text",
                "text": {"content": content}
            }
            res = await self._post(self.wecom_webhook, payload)
            success = success or res

        return success

    async def send_daily_pick_card(
        self,
        symbol: str,
        reason: str,
        target_price: float,
        stop_loss: float,
        score: int
    ) -> bool:
        """
        Send structured Daily Pick recommendation card.
        """
        content = (
            f"📊 【AI 美股 bStocks 每日精选潜力标的】\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 标的代号: {symbol}\n"
            f"⭐️ 技术综合评分: {score} / 100\n"
            f"🎯 建议目标价: ${target_price:.2f}\n"
            f"🛡️ 建议止损位: ${stop_loss:.2f}\n"
            f"💡 核心研判逻辑:\n{reason}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        return await self.send_text(content)

    async def send_position_alert(
        self,
        symbol: str,
        buy_price: float,
        curr_price: float,
        pnl_pct: float,
        alert_type: str,
        extra_note: str = ""
    ) -> bool:
        """
        Send position price alert card (Periodic check / Take Profit / Stop Loss / Ladder alert).
        """
        emoji_map = {
            "take_profit": "🚀 【阶段冲高 / 止盈提醒】",
            "stop_loss": "⚠️ 【安全防线 / 止损预警】",
            "periodic": "⏱️ 【持仓定时巡检汇报】",
            "ladder": "🔥 【阶梯涨幅达成提醒】"
        }
        title = emoji_map.get(alert_type, "📢 【持仓变动提醒】")
        pnl_str = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"

        content = (
            f"{title}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 标的代号: {symbol}\n"
            f"💵 买入成本: ${buy_price:.2f}\n"
            f"📈 当前最新价: ${curr_price:.2f}\n"
            f"📊 当前浮动盈亏: {pnl_str}\n"
            + (f"💬 说明: {extra_note}\n" if extra_note else "")
            + f"━━━━━━━━━━━━━━━━━━━━"
        )
        return await self.send_text(content)

    async def _post(self, url: str, payload: Dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                logger.warning(f"Webhook post failed [{resp.status_code}]: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Error posting notification to {url}: {e}")
            return False

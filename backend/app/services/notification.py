import inspect
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
        self._feishu_webhook = feishu_webhook or settings.FEISHU_WEBHOOK_URL or ""
        self._wecom_webhook = wecom_webhook or getattr(settings, "WECOM_WEBHOOK_URL", "") or ""
        self.timeout = timeout

    @property
    def feishu_webhook(self) -> str:
        return self._feishu_webhook or ""

    @feishu_webhook.setter
    def feishu_webhook(self, val: Optional[str]):
        self._feishu_webhook = val or ""

    @property
    def feishu_webhook_url(self) -> str:
        return self._feishu_webhook or ""

    @feishu_webhook_url.setter
    def feishu_webhook_url(self, val: Optional[str]):
        self._feishu_webhook = val or ""

    @property
    def wecom_webhook(self) -> str:
        return self._wecom_webhook or ""

    @wecom_webhook.setter
    def wecom_webhook(self, val: Optional[str]):
        self._wecom_webhook = val or ""

    @property
    def wecom_webhook_url(self) -> str:
        return self._wecom_webhook or ""

    @wecom_webhook_url.setter
    def wecom_webhook_url(self, val: Optional[str]):
        self._wecom_webhook = val or ""

    async def send_channel_text(self, channel: str, content: str) -> bool:
        """Explicit channel result for persisted, independently retryable deliveries."""
        if channel == 'feishu' and self.feishu_webhook:
            return await self._post(self.feishu_webhook, {'msg_type':'text','content':{'text':content}})
        if channel == 'wecom' and self.wecom_webhook:
            return await self._post(self.wecom_webhook, {'msgtype':'text','text':{'content':content}})
        return False

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
        extra_note: str = "",
        trend_analysis: Optional[Dict[str, Any]] = None,
        pnl_usd: Optional[float] = None
    ) -> bool:
        """
        Send position price alert card (Periodic check / Take Profit / Stop Loss / Ladder alert),
        with direct Binance Convert action button and optional AI trend analysis block.
        """
        emoji_map = {
            "take_profit": "🚀 【阶段冲高 / 止盈提醒】",
            "stop_loss": "🚨 【破位警报 / 严格止损】",
            "periodic": "⏱️ 【持仓定时巡检汇报】",
            "ladder": "🔥 【阶梯涨幅达成提醒】"
        }
        title = emoji_map.get(alert_type, "📢 【持仓变动提醒】")
        equity = symbol.startswith('EQ_')
        unit = 'USD' if equity else 'USDT'
        price_label = '买卖中间价估值（非可成交价）' if equity else '当前最新价'
        extra_note = f'{extra_note}；价格及盈亏单位 {unit}'
        pnl_sign = "+" if pnl_pct >= 0 else ""
        usd_str = f" (${pnl_usd:+.2f})" if pnl_usd is not None and abs(pnl_usd) > 0.001 else ""
        pnl_display = f"{pnl_sign}{pnl_pct:.2f}%{usd_str}"
        pnl_color_tag = "🟢" if pnl_pct >= 0 else "🔴"

        color = "red" if alert_type == "stop_loss" else ("green" if pnl_pct >= 0 else "red")
        if alert_type == "periodic" and pnl_pct >= 0:
            color = "green"
        elif alert_type == "periodic" and pnl_pct < 0:
            color = "orange"

        success = False

        # Build Feishu card elements
        elements = [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**买入成本 ({unit}):**\n${buy_price:.2f}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**{price_label} ({unit}):**\n**${curr_price:.2f}**"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**实时盈亏:**\n{pnl_color_tag} **{pnl_display}**"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**巡检状态:**\n{extra_note or '短线实时盯盘'}"}}
                ]
            }
        ]

        trend_plain_block = ""
        if trend_analysis:
            reason = trend_analysis.get("reason", "短线蓄势中")
            tp = trend_analysis.get("target_price")
            sl = trend_analysis.get("stop_loss_price")
            tp_str = f"${float(tp):.2f}" if tp else "--"
            sl_str = f"${float(sl):.2f}" if sl else "--"
            elements.extend([
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"🔮 **AI 短线未来趋势研判:**\n{reason}\n🎯 **研判止盈目标:** {tp_str}  |  🛡️ **防守止损位:** {sl_str}"
                    }
                }
            ])
            trend_plain_block = (
                f"🔮 【AI 短线未来趋势研判】\n"
                f"💡 逻辑: {reason}\n"
                f"🎯 研判目标: {tp_str} | 🛡️ 防守支撑: {sl_str}\n"
            )

        elements.extend([
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "⚡ 短线交易纪律：快进快出，保护本金，严格执行止盈止损"}]
            }
        ])

        plain_text = (
            f"{title}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 标的代号: {symbol}\n"
            f"💵 买入成本: {buy_price:.2f} {unit}\n"
            f"📈 {price_label}: {curr_price:.2f} {unit}\n"
            f"📊 实时盈亏: {pnl_display}\n"
            + (f"💬 状态说明: {extra_note}\n" if extra_note else "")
            + (f"━━━━━━━━━━━━━━━━━━━━\n{trend_plain_block}" if trend_plain_block else "")
            + f"━━━━━━━━━━━━━━━━━━━━"
        )

        # Feishu Interactive Card
        if self.feishu_webhook:
            card_payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": f"{title} · {symbol}"},
                        "template": color
                    },
                    "elements": elements
                }
            }
            res = await self._post(self.feishu_webhook, card_payload)
            if not res:
                # Fallback to plain text
                res = await self._post(self.feishu_webhook, {"msg_type": "text", "content": {"text": plain_text}})
            success = success or res

        # WeCom webhook
        if self.wecom_webhook:
            res = await self._post(self.wecom_webhook, {"msg_type": "text", "text": {"content": plain_text}})
            success = success or res

        return success

    async def _post(self, url: str, payload: Dict[str, Any]) -> bool:
        if not url:
            return False
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if inspect.isawaitable(data):
                            data = await data
                        if isinstance(data, dict):
                            if "code" in data and data["code"] != 0:
                                logger.warning('Webhook returned a nonzero code')
                                return False
                            if "StatusCode" in data and data["StatusCode"] != 0:
                                logger.warning('Webhook returned a nonzero status')
                                return False
                            if "errcode" in data and data["errcode"] != 0:
                                logger.warning('Webhook returned a nonzero errcode')
                                return False
                    except Exception:
                        return False
                    return isinstance(data, dict) and any(data.get(k) == 0 for k in ('code','StatusCode','errcode'))
                logger.warning('Webhook request failed (HTTP %s)', resp.status_code)
                return False
        except Exception as e:
            logger.error('Notification request failed (%s)', type(e).__name__)
            return False

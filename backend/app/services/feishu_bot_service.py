import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional
import aiosqlite
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from backend.app.config import settings
from backend.app.services.binance_client import BinanceClient
from backend.app.services.quant_screener import QuantScreener
from backend.app.services.analysis_agent import AnalysisAgent
from backend.app.services.position_monitor import PositionMonitor

logger = logging.getLogger(__name__)

class FeishuBotService:
    """
    Feishu Two-Way Conversational Bot Service using official WebSocket Long Connection:
    - Real-time command handling: '持仓', '保本', '止损 245', '止盈 260', '选股', '分析 TSLA'
    - Natural language trading consultation via LLM AnalysisAgent
    - Interactive rich cards and instant confirmation
    """

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        binance_client: Optional[BinanceClient] = None,
        quant_screener: Optional[QuantScreener] = None,
        analysis_agent: Optional[AnalysisAgent] = None,
        position_monitor: Optional[PositionMonitor] = None,
        db_path: Optional[str] = None
    ):
        self.app_id = app_id or settings.FEISHU_APP_ID
        self.app_secret = app_secret or settings.FEISHU_APP_SECRET
        self.binance_client = binance_client or BinanceClient()
        self.quant_screener = quant_screener
        self.analysis_agent = analysis_agent
        self.position_monitor = position_monitor
        self.db_path = db_path or settings.DB_PATH

        self._token_cache: str = ""
        self._token_expire: float = 0
        self._ws_client: Optional[lark.ws.Client] = None
        self._ws_thread: Optional[threading.Thread] = None
        self.is_connected: bool = False
        self._processed_msg_ids: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_credentials(self, app_id: str, app_secret: str):
        need_restart = (app_id != self.app_id or app_secret != self.app_secret)
        self.app_id = app_id
        self.app_secret = app_secret
        if need_restart and self.is_connected:
            self.stop()
            self.start()

    async def get_tenant_access_token(self) -> Optional[str]:
        if not self.app_id or not self.app_secret:
            return None
        now = time.time()
        if self._token_cache and now < self._token_expire:
            return self._token_cache

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret
                })
                data = res.json()
                if data.get("code") == 0:
                    self._token_cache = data["tenant_access_token"]
                    self._token_expire = now + float(data.get("expire", 7200)) - 180
                    return self._token_cache
                else:
                    logger.error(f"Failed to get Feishu tenant_access_token: {data}")
                    return None
        except Exception as e:
            logger.error(f"Exception getting Feishu tenant_access_token: {e}")
            return None

    def start(self):
        if not self.app_id or not self.app_secret:
            logger.info("Feishu App credentials not configured; skipping WebSocket startup.")
            return

        try:
            # Capture or get current event loop
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.get_event_loop()

            def on_msg_received(data: P2ImMessageReceiveV1):
                try:
                    msg = data.event.message
                    msg_id = msg.message_id
                    if msg_id in self._processed_msg_ids:
                        return
                    self._processed_msg_ids.add(msg_id)
                    if len(self._processed_msg_ids) > 1000:
                        self._processed_msg_ids.clear()

                    chat_id = msg.chat_id
                    sender_id = data.event.sender.sender_id.open_id if data.event.sender and data.event.sender.sender_id else ""
                    content_str = msg.content or "{}"
                    content_dict = json.loads(content_str)
                    raw_text = content_dict.get("text", "").strip()

                    # Schedule async processing on the main event loop
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.handle_incoming_text(raw_text, msg_id, chat_id, sender_id),
                            self._loop
                        )
                    else:
                        asyncio.run(self.handle_incoming_text(raw_text, msg_id, chat_id, sender_id))
                except Exception as e:
                    logger.error(f"Error handling Feishu message event: {e}", exc_info=True)

            event_builder = lark.EventDispatcherHandler.builder("", "")
            event_builder.register_p2_im_message_receive_v1(on_msg_received)
            handler = event_builder.build()

            self._ws_client = lark.ws.Client(
                app_id=self.app_id,
                app_secret=self.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO
            )

            self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
            self._ws_thread.start()
            self.is_connected = True
            logger.info("Feishu Bot WebSocket client thread launched.")
        except Exception as e:
            logger.error(f"Failed to start Feishu Bot WebSocket: {e}", exc_info=True)
            self.is_connected = False

    def _run_ws(self):
        try:
            if self._ws_client:
                self._ws_client.start()
        except Exception as e:
            logger.error(f"Feishu Bot WebSocket stopped: {e}")
            self.is_connected = False

    def stop(self):
        self.is_connected = False
        self._ws_client = None

    async def reply_message(self, message_id: str, content: Any, msg_type: str = "text") -> bool:
        token = await self.get_tenant_access_token()
        if not token:
            logger.warning("Cannot reply to Feishu message: missing access token.")
            return False

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }

        if msg_type == "text":
            body = {"msg_type": "text", "content": json.dumps({"text": str(content)})}
        elif msg_type == "interactive":
            body = {"msg_type": "interactive", "content": json.dumps(content)}
        else:
            body = {"msg_type": "text", "content": json.dumps({"text": str(content)})}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, json=body)
                resp = res.json()
                if resp.get("code") == 0:
                    return True
                else:
                    logger.error(f"Feishu reply error: {resp}")
                    return False
        except Exception as e:
            logger.error(f"Exception replying to Feishu message: {e}")
            return False

    async def handle_incoming_text(self, raw_text: str, message_id: str, chat_id: str, sender_id: str):
        """Parse commands or pass to LLM natural language assistant."""
        clean_text = re.sub(r"@_user_\d+", "", raw_text).strip()
        if not clean_text:
            return

        cmd = clean_text.lower()

        # 1. Check Positions (持仓 / 仓位 / 查仓 / 我的持仓)
        if any(w in cmd for w in ["持仓", "仓位", "查仓", "查持仓", "我的持仓", "positions"]):
            await self._handle_positions_query(message_id)
            return

        # 2. Break-even (开启保本 / 保本 / 保本防守)
        if "保本" in cmd:
            await self._handle_set_breakeven(message_id, clean_text)
            return

        # 3. Stop Loss adjustment (调止损 / 改止损 / 止损)
        if any(w in cmd for w in ["止损", "改止损", "调止损", "设止损"]):
            await self._handle_set_stop_loss(message_id, clean_text)
            return

        # 4. Take Profit adjustment (调止盈 / 改止盈 / 止盈)
        if any(w in cmd for w in ["止盈", "改止盈", "调止盈", "设止盈"]):
            await self._handle_set_take_profit(message_id, clean_text)
            return

        # 5. AI Recommendations (选股 / 今日推荐 / 推荐)
        if any(w in cmd for w in ["选股", "今日推荐", "推荐标的", "推荐", "picks"]):
            await self._handle_picks_query(message_id)
            return

        # 6. AI Deep Dive (分析 / 研判 e.g. '分析 TSLA' or '研判 NVDA')
        if any(w in clean_text for w in ["分析", "研判", "诊断", "看下", "查下"]):
            handled = await self._handle_symbol_analysis(message_id, clean_text)
            if handled:
                return

        # 7. Help (帮助 / 指令 / 菜单)
        if any(w in cmd for w in ["帮助", "指令", "菜单", "功能", "help"]):
            await self._handle_help(message_id)
            return

        # 8. Fallback to conversational LLM short-term assistant
        await self._handle_llm_conversation(message_id, clean_text)

    async def _get_active_positions(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM positions WHERE is_active = 1")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def _handle_positions_query(self, message_id: str):
        positions = await self._get_active_positions()
        if not positions:
            await self.reply_message(
                message_id,
                "📊 当前没有正在监控中的持仓标的。\n您可在 Web 控制台 (https://bot.smlershou.top) 添加，或在系统配置同步币安现货持仓。"
            )
            return

        elements = []
        total_pnl_usd = 0.0

        for pos in positions:
            sym = pos["symbol"]
            buy = float(pos["buy_price"])
            qty = float(pos["quantity"])
            curr = buy
            try:
                curr = await self.binance_client.get_ticker_price(sym)
            except Exception:
                pass

            pnl_pct = ((curr - buy) / buy) * 100.0 if buy > 0 else 0.0
            pnl_usd = (curr - buy) * qty
            total_pnl_usd += pnl_usd

            pnl_color = "🟢" if pnl_pct >= 0 else "🔴"
            tp_str = f"${float(pos['take_profit_price']):.2f}" if pos.get("take_profit_price") else "未设"
            sl_str = f"${float(pos['stop_loss_price']):.2f}" if pos.get("stop_loss_price") else "未设"

            elements.append({
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**标的代号:** {sym}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**实时盈亏:** {pnl_color} **{pnl_pct:+.2f}%** (${pnl_usd:+.2f})"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**成本/现价:** ${buy:.2f} / **${curr:.2f}**"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**止盈/止损:** {tp_str} / {sl_str}"}}
                ]
            })
            elements.append({"tag": "hr"})

        summary_color = "green" if total_pnl_usd >= 0 else "red"
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 实时持仓看板 · 共 {len(positions)} 个监控标的"},
                "template": summary_color
            },
            "elements": elements + [
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "💡 提示: 您可以直接回复「开启保本」或「把 AMZNB 止损改到 245」快捷调整风控！"}]
                }
            ]
        }
        await self.reply_message(message_id, card, msg_type="interactive")

    async def _handle_set_breakeven(self, message_id: str, text: str):
        positions = await self._get_active_positions()
        if not positions:
            await self.reply_message(message_id, "当前无活跃持仓，无需开启保本。")
            return

        target_sym = None
        for p in positions:
            raw_sym = p["symbol"].replace("BUSDT", "").replace("USDT", "")
            if raw_sym.lower() in text.lower() or p["symbol"].lower() in text.lower():
                target_sym = p["symbol"]
                break

        async with aiosqlite.connect(self.db_path) as db:
            updated = []
            for p in positions:
                if target_sym is None or p["symbol"] == target_sym:
                    buy = float(p["buy_price"])
                    await db.execute("UPDATE positions SET stop_loss_price = ? WHERE id = ?", (buy, p["id"]))
                    updated.append(f"🔹 {p['symbol']}: 止损线已平推至买入成本价 **${buy:.2f}**")
            await db.commit()

        res_text = "🛡️ **已成功开启保本防护！**\n\n" + "\n".join(updated) + "\n\n💡 短线铁律：绝不让盈利单倒亏！"
        await self.reply_message(message_id, res_text)

    async def _handle_set_stop_loss(self, message_id: str, text: str):
        positions = await self._get_active_positions()
        if not positions:
            await self.reply_message(message_id, "当前暂无持仓可修改止损。")
            return

        nums = re.findall(r"\d+(?:\.\d+)?", text)
        if not nums:
            await self.reply_message(message_id, "❓ 未识别到目标止损价格。格式示例: 「把 AMZNB 止损改到 245」或「止损 245」")
            return
        target_price = float(nums[-1])

        target_pos = None
        for p in positions:
            raw_sym = p["symbol"].replace("BUSDT", "").replace("USDT", "")
            if raw_sym.lower() in text.lower() or p["symbol"].lower() in text.lower():
                target_pos = p
                break
        if not target_pos:
            target_pos = positions[0]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE positions SET stop_loss_price = ? WHERE id = ?", (target_price, target_pos["id"]))
            await db.commit()

        buy = float(target_pos["buy_price"])
        dist_pct = ((target_price - buy) / buy) * 100.0 if buy > 0 else 0.0
        reply = (
            f"✅ **已成功调整止损防线！**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 标的代号: **{target_pos['symbol']}**\n"
            f"💵 买入成本: ${buy:.2f}\n"
            f"🛡️ 新止损防守价: **${target_price:.2f}** ({dist_pct:+.2f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ 实时盯盘引擎已同步更新此防线，跌破将第一时间告警！"
        )
        await self.reply_message(message_id, reply)

    async def _handle_set_take_profit(self, message_id: str, text: str):
        positions = await self._get_active_positions()
        if not positions:
            await self.reply_message(message_id, "当前暂无持仓可修改止盈。")
            return

        nums = re.findall(r"\d+(?:\.\d+)?", text)
        if not nums:
            await self.reply_message(message_id, "❓ 未识别到目标止盈价格。格式示例: 「把 AMZNB 止盈改到 260」或「止盈 260」")
            return
        target_price = float(nums[-1])

        target_pos = None
        for p in positions:
            raw_sym = p["symbol"].replace("BUSDT", "").replace("USDT", "")
            if raw_sym.lower() in text.lower() or p["symbol"].lower() in text.lower():
                target_pos = p
                break
        if not target_pos:
            target_pos = positions[0]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE positions SET take_profit_price = ? WHERE id = ?", (target_price, target_pos["id"]))
            await db.commit()

        buy = float(target_pos["buy_price"])
        dist_pct = ((target_price - buy) / buy) * 100.0 if buy > 0 else 0.0
        reply = (
            f"🎯 **已成功调整止盈目标！**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 标的代号: **{target_pos['symbol']}**\n"
            f"💵 买入成本: ${buy:.2f}\n"
            f"🚀 新目标止盈价: **${target_price:.2f}** ({dist_pct:+.2f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ 冲高达到目标时将第一时间给您推送落袋为安卡片！"
        )
        await self.reply_message(message_id, reply)

    async def _handle_picks_query(self, message_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM daily_picks ORDER BY id DESC LIMIT 3")
            picks = [dict(r) for r in await cursor.fetchall()]

        if not picks:
            await self.reply_message(
                message_id,
                "🤖 当前暂无生成的 AI 选股记录。\n您可以回复「分析 TSLA」单独研判某只股票，或在控制台点击「即刻启动 AI 选股」。"
            )
            return

        elements = []
        for p in picks:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🎯 **【{p['symbol']}】**\n💡 **逻辑:** {p.get('reason', '')}\n🚀 **研判目标:** ${float(p.get('target_price', 0)):.2f}"
                }
            })
            elements.append({"tag": "hr"})

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "🔮 AI 美股短线潜力标的推荐"},
                "template": "indigo"
            },
            "elements": elements + [
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "短线交易原则: 快进快出锁定波段，严格防守止损！"}]}
            ]
        }
        await self.reply_message(message_id, card, msg_type="interactive")

    async def _handle_symbol_analysis(self, message_id: str, text: str) -> bool:
        words = re.findall(r"[a-zA-Z]+", text)
        clean_words = [w.upper() for w in words if w.upper() not in ["AI", "VS", "OK", "BUY", "SELL", "HELP"]]
        if not clean_words:
            return False

        sym = clean_words[0]
        if not sym.endswith("USDT"):
            if not sym.endswith("B"):
                sym = sym + "BUSDT"
            else:
                sym = sym + "USDT"

        await self.reply_message(message_id, f"🔍 正在调取币安 {sym} 实时行情并召唤大模型进行 1~3 天短线推演，请稍候...")

        try:
            klines = await self.binance_client.get_klines(sym, interval="1d", limit=40)
            if not klines or len(klines) < 5:
                klines = await self.binance_client.get_klines(sym, interval="1h", limit=40)

            if not klines:
                await self.reply_message(message_id, f"未能获取到 {sym} 的币安行情数据，请确认标的代码是否正确。")
                return True

            df = self.quant_screener.calculate_indicators(klines)
            scored = self.quant_screener.score_stock(df)
            analysis = await self.analysis_agent.analyze_candidate(
                symbol=sym,
                score=scored.get("score", 0),
                signals=scored.get("signals", []),
                metrics=scored.get("metrics", {})
            )

            curr_price = await self.binance_client.get_ticker_price(sym)
            tp_str = f"${float(analysis['target_price']):.2f}" if analysis.get("target_price") else "--"
            sl_str = f"${float(analysis['stop_loss_price']):.2f}" if analysis.get("stop_loss_price") else "--"

            card = {
                "header": {
                    "title": {"tag": "plain_text", "content": f"🔮 {sym} 短线趋势与量化研判"},
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "fields": [
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**当前最新价:**\n${curr_price:.2f}"}},
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**量化形态评分:**\n{scored.get('score', 0)} / 100"}},
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**建议目标价:**\n{tp_str}"}},
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**防守止损位:**\n{sl_str}"}}
                        ]
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"💡 **AI 核心短线推演逻辑:**\n{analysis.get('reason', '')}"}
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [{"tag": "plain_text", "content": "⚡ 1~3 天短线策略，重视均线与支撑有效性，严格执行防守！"}]
                    }
                ]
            }
            await self.reply_message(message_id, card, msg_type="interactive")
            return True
        except Exception as e:
            logger.error(f"Error analyzing symbol {sym}: {e}")
            await self.reply_message(message_id, f"研判 {sym} 时出现异常: {e}")
            return True

    async def _handle_help(self, message_id: str):
        help_text = (
            "🤖 **Hermes 投资助手 · 短线高频量化指令指南**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💬 **您可以随时在聊天框给我发送：**\n\n"
            "🔹 **`持仓` / `仓位`**：查看当前所有标的最新价、盈亏率及防守线\n"
            "🔹 **`开启保本`**：自动将所有/指定持仓止损拉升至买入成本价\n"
            "🔹 **`止损 245` 或 `AMZNB 止损 245`**：快速调整止损防守价\n"
            "🔹 **`止盈 260` 或 `AMZNB 止盈 260`**：快速调整止盈目标价\n"
            "🔹 **`选股` / `今日推荐`**：获取最新全市场多指标初筛潜力标的\n"
            "🔹 **`分析 TSLA` / `研判 NVDA`**：即时调取行情与大模型 1~3 天短线推演\n"
            "🔹 **直接提问**：任何美股短线指标、技术形态、盘面疑问自由交流\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ 严格短线纪律：快进快出，保护利润，坚决不扛单！"
        )
        await self.reply_message(message_id, help_text)

    async def _handle_llm_conversation(self, message_id: str, text: str):
        """Use AnalysisAgent LLM for natural trading chat."""
        if not self.analysis_agent:
            await self.reply_message(message_id, "您好！我是您的短线量化盯盘助手，回复「帮助」查看可用快捷指令。")
            return

        positions = await self._get_active_positions()
        pos_summary = ", ".join([f"{p['symbol']} (成本${p['buy_price']:.2f})" for p in positions]) if positions else "暂无持仓"

        system_prompt = (
            "你是一个专业的币安美股 bStocks 短线高频量化交易助理。\n"
            "用户当前的持仓状态为: " + pos_summary + "。\n"
            "【短线核心法则】:\n"
            "1. 绝不长线死扛，主做 1~3 天短线波段爆发；\n"
            "2. 浮盈 +3%~+8% 坚决锁定利润，触及 -2%~-4% 坚决止损，有利润及时保本；\n"
            "3. 你的回答必须专业、言简意赅、充满短线交易纪律与风险控制意识，杜绝无意义套话。\n"
            "请直接针对用户的提问进行解答："
        )

        try:
            ans = await self.analysis_agent.ask_llm(text, system_prompt=system_prompt)
            if not ans:
                if any(greet in text.lower() for greet in ["你好", "hello", "hi", "在吗", "在不在", "早上好", "晚上好"]):
                    ans = (
                        f"👋 您好！我是您的短线量化盯盘助理 Hermes ⚡\n"
                        f"📊 当前活跃持仓监控: {pos_summary}\n\n"
                        "💬 您可以随时发送：\n"
                        "• 「持仓」查看最新现价与实时盈亏\n"
                        "• 「开启保本」自动将止损平推至成本价\n"
                        "• 「止损 245」微调防守位\n"
                        "• 「分析 TSLA」获取 1~3 天短线推演\n"
                        "• 或直接与我交流短线盘面与交易纪律！"
                    )
                else:
                    ans = "收到您的消息。如需查持仓请发送「持仓」，调参数可发送「止损 245」，分析标的可发送「分析 标的代码」，或回复「帮助」查看完整指令。"
            await self.reply_message(message_id, ans)
        except Exception as e:
            logger.error(f"Error in LLM conversation: {e}")
            fallback_msg = (
                f"👋 您好！我是您的短线量化助理 Hermes。\n"
                f"当前活跃持仓: {pos_summary}。\n"
                "可直接回复「持仓」、「保本」或「帮助」执行量化风控指令。"
            )
            await self.reply_message(message_id, fallback_msg)

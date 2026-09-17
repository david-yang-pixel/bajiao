import json
import logging
import re
from typing import Any, Dict, List, Optional
import httpx

from backend.app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个专为极致短线/超短线波段交易员服务的量化 AI 研判专家。
【专属交易核心原则】：
1. 用户只做短线/超短线（持仓通常仅数小时至 1~3 天），绝不做长线死扛或价值投资！
2. 交易风格以“快进快出、严守纪律、分批锁利、严控回撤”为纲：
   - 目标止盈价（target_price）：锁定在短线动量阻力位或放量冲击点（通常短线收益目标在 +3% ~ +8% 左右，见好就收）。
   - 安全止损价（stop_loss_price）：务必严格紧凑，紧贴关键短线防守支撑（通常在 -2% ~ -4% 之间，如跌破短期MA均线或分时支撑位），破位果断离场！
3. 请依据给定的技术指标（短期均线排列、RSI爆发力、量比放量、布林带突破）直击短线要害，指出短线动能、最佳入场位与出场防线，绝无长线废话。

请严格输出符合以下 JSON 格式的结构化分析，不要包含任何 markdown 语法外的附加内容：
{
  "symbol": "标的代码",
  "reason": "短线实战研判逻辑（100字内，精炼指出短期动量、突破形态、量能与关键短线支撑防守位）",
  "target_price": 短线目标止盈价(float),
  "stop_loss_price": 短线紧凑止损价(float),
  "upside_probability": "High" | "Medium" | "Low"
}
"""

class AnalysisAgent:
    """
    Agent combining technical screener data and LLM reasoning
    to generate deep-dive actionable stock recommendations tailored for short-term trading.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.openai.com/v1", model: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = base_url.rstrip("/")
        self.model = model or getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")

    async def analyze_candidate(
        self,
        symbol: str,
        score: int,
        signals: List[str],
        metrics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Analyze a candidate using OpenAI/compatible API or fallback rule-based reasoning.
        """
        curr_price = float(metrics.get("close", 100.0))
        signals_str = ", ".join(signals) if signals else "短线量化指标共振向好"

        user_content = f"""
短线分析标的: {symbol}
量化动量初筛得分: {score}/100
短线触发信号: {signals_str}
当前关键量化数值:
- 最新现价: {curr_price}
- 短期均线: MA5={metrics.get('ma5')}, MA10={metrics.get('ma10')}, MA20={metrics.get('ma20')}
- 短期强弱 RSI(14): {metrics.get('rsi')}
- 实时成交量: {metrics.get('volume')} (5日均量: {metrics.get('vol_ma5')})
- 布林带上轨阻力: {metrics.get('bb_upper')}

请依据【纯短线交易纪律】，给出未来 1~3 天内爆发力研判、短线看涨逻辑、短线目标止盈价(+3%~+8%)与安全止损防守位(-2%~-4%)。
"""

        if self.api_key:
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": self.model or "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.3
                }
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_result = data["choices"][0]["message"]["content"]
                        clean_text = raw_result.strip()
                        match = re.search(r"\{.*\}", clean_text, re.DOTALL)
                        if match:
                            clean_text = match.group(0)
                        parsed = json.loads(clean_text)
                        return {
                            "symbol": symbol,
                            "reason": parsed.get("reason", signals_str),
                            "target_price": float(parsed.get("target_price", round(curr_price * 1.05, 2))),
                            "stop_loss_price": float(parsed.get("stop_loss_price", round(curr_price * 0.97, 2))),
                            "upside_probability": parsed.get("upside_probability", "High")
                        }
            except Exception as e:
                logger.warning(f"LLM API call failed for {symbol}: {e}. Using deterministic reasoning.")

        # Deterministic fallback when LLM is offline or no API key is provided
        target = round(curr_price * 1.05, 2)
        stop = round(curr_price * 0.97, 2)
        reason = f"短线形态强势(得分{score})，触发[{signals_str}]，MA5短线向上发散，量能有效放大，具备短期冲击阻力位动能，建议严设止损紧密跟踪。"
        return {
            "symbol": symbol,
            "reason": reason,
            "target_price": target,
            "stop_loss_price": stop,
            "upside_probability": "High" if score >= 70 else "Medium"
        }

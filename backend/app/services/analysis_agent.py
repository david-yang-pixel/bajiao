import json
import logging
from typing import Any, Dict, List, Optional
import httpx

from backend.app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个资深美股与加密货币代币化资产量化分析专家。
请根据提供的代币代码、技术面量化指标（均线、RSI、成交量、布林带）以及上下文，研判该资产在未来 1~3 天内是否有进一步上涨潜力。
请严格输出符合以下 JSON 格式的结构化分析，不要包含任何 markdown 语法外的附加内容：
{
  "symbol": "标的代码",
  "reason": "看涨/观望核心逻辑（100字内通俗易懂分析，指出支撑位、量能与突破形态）",
  "target_price": 目标止盈价(float),
  "stop_loss_price": 建议安全止损价(float),
  "upside_probability": "High" | "Medium" | "Low"
}
"""

class AnalysisAgent:
    """
    Agent combining technical screener data and LLM reasoning
    to generate deep-dive actionable stock recommendations.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = base_url.rstrip("/")

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
        signals_str = ", ".join(signals) if signals else "量化技术指标向好"

        user_content = f"""
资产代码: {symbol}
量化初筛得分: {score}/100
触发技术信号: {signals_str}
最新关键数值:
- 收盘价: {curr_price}
- MA5: {metrics.get('ma5')}, MA10: {metrics.get('ma10')}, MA20: {metrics.get('ma20')}
- RSI(14): {metrics.get('rsi')}
- 最新成交量: {metrics.get('volume')} (5日均量: {metrics.get('vol_ma5')})
- 布林带上轨: {metrics.get('bb_upper')}

请给出未来1-3天的走势研判、看涨理由、目标止盈价与安全止损价。
"""

        if self.api_key:
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": "gpt-4o-mini",
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
                        parsed = json.loads(raw_result)
                        return {
                            "symbol": symbol,
                            "reason": parsed.get("reason", signals_str),
                            "target_price": float(parsed.get("target_price", round(curr_price * 1.05, 2))),
                            "stop_loss_price": float(parsed.get("stop_loss_price", round(curr_price * 0.96, 2))),
                            "upside_probability": parsed.get("upside_probability", "High")
                        }
            except Exception as e:
                logger.warning(f"LLM API call failed for {symbol}: {e}. Using deterministic reasoning.")

        # Deterministic fallback when LLM is offline or no API key is provided
        target = round(curr_price * 1.06, 2)
        stop = round(curr_price * 0.96, 2)
        reason = f"技术面表现强势(得分{score})，触发[{signals_str}]，短期均线呈多头排列，量能有效放大，具备进一步向上冲击阻力位动能。"
        return {
            "symbol": symbol,
            "reason": reason,
            "target_price": target,
            "stop_loss_price": stop,
            "upside_probability": "High" if score >= 70 else "Medium"
        }

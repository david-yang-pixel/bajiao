import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { CONFIG, readToken } from "./config.js";
import { ToolLog, wrapTool } from "./toolLog.js";

const SYMBOL = Type.Object({ symbol: Type.String({ description: "正股代码，如 NVDA" }) });
const EMPTY = Type.Object({});

async function post(
  fetchImpl: typeof fetch,
  token: string,
  name: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<string> {
  const res = await fetchImpl(`${CONFIG.mainUrl}/internal/tools/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
    signal,
  });
  const text = await res.text();
  if (!res.ok) {
    let detail: string | undefined;
    try {
      const d = JSON.parse(text)?.detail;
      if (d !== undefined) detail = typeof d === "string" ? d : JSON.stringify(d);
    } catch {
      /* 非 JSON 响应 */
    }
    throw new Error(detail ?? (res.statusText || `HTTP ${res.status}`));
  }
  return text;
}

export function createInternalTools(
  log: ToolLog,
  fetchImpl: typeof fetch = fetch,
  tokenProvider: () => string = readToken,
): ToolDefinition[] {
  const mk = (name: string, label: string, description: string, parameters: any, bySymbol: boolean) =>
    wrapTool(log, {
      name,
      label,
      description,
      parameters,
      run: (params, signal) =>
        post(fetchImpl, tokenProvider(), name, bySymbol ? { symbol: params.symbol } : {}, signal),
    });
  return [
    mk("get_factors", "获取因子", "获取标的的技术/动量/波动率等因子与 ATR、参考价（正股 USD）", SYMBOL, true),
    mk("get_token_quote", "获取代币报价", "获取该标的币安代币现货报价、可交易性与折算参考（USDT）", SYMBOL, true),
    mk("get_crypto_context", "加密市场环境", "获取加密市场整体环境", EMPTY, false),
    mk("get_market_context", "美股市场环境",
      "获取美股大盘环境：SPY/QQQ 近 5/20 日涨跌与是否站上 20 日均线，以及未来 7 天的宏观事件日程（FOMC 利率决议）",
      EMPTY, false),
  ];
}

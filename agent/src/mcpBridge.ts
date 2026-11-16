import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport, getDefaultEnvironment } from "@modelcontextprotocol/sdk/client/stdio.js";
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { ToolLog, wrapTool } from "./toolLog.js";

export type McpServerSpec = {
  name: "openbb" | "alpaca";
  command: string;
  args: string[];
  env: Record<string, string>;
  allow: (toolName: string) => boolean;
};

export interface McpKeys {
  alpaca_api_key?: string;
  alpaca_secret_key?: string;
  fred_api_key?: string;
}

// OpenBB — values measured against openbb-mcp-server 2.0.1 (openbb-core 2.0.1, fastmcp 4.2.0) with
// openbb-equity/derivatives/index/economy/sec/fred 2.0.0 and openbb-yfinance 2.1.0 installed
// (openbb-mcp-server alone exposes no data tools). Unrestricted it lists 207 tools; the
// --allowed-categories flag below cuts that to 98 and the allow-set to 11. Every category the
// skill needs is available — none is "不可用".
export const OPENBB_ARGS: readonly string[] = [
  "--transport", "stdio",
  "--allowed-categories", "equity,derivatives,index,economy,yfinance",
];
// openbb-core reads provider credentials from any env var whose lower-cased name is a credential key.
export const OPENBB_FRED_ENV = "FRED_API_KEY";
export const OPENBB_ALLOWED_TOOLS: ReadonlySet<string> = new Set([
  // yfinance 分析师评级 / 目标价
  "equity_estimates_price_target",
  "equity_estimates_consensus",
  "yfinance_estimates_recommendations",
  // yfinance 财报日历与历史
  "equity_calendar_earnings",
  "yfinance_company_calendar",
  "equity_fundamental_historical_eps",
  // yfinance 期权链
  "derivatives_options_chains",
  // yfinance VIX / 指数行情（symbol 如 ^VIX、^GSPC）
  "index_price_historical",
  // SEC 8-K（provider=sec, form_type=8-K）与 Form 4（provider=sec）
  "equity_fundamental_filings",
  "equity_ownership_insider_trading",
  // FRED 发布日历（provider=fred）
  "economy_calendar",
]);
// Allowed tools that need the FRED key; dropped when it is missing. Empty: openbb-fred's
// economic calendar has require_credentials=False (it reads fred.stlouisfed.org/releases/calendar),
// verified to return releases with no key set.
export const OPENBB_FRED_KEY_TOOLS: ReadonlySet<string> = new Set();

// Alpaca — measured against alpaca-mcp-server 2.4.1 (pins fastmcp 3.4.7). It cannot share a venv with
// openbb-mcp-server (fastmcp>=4.0.9): pip then falls back to alpaca-mcp-server 2.3.0, which crashes at
// import under fastmcp 4 — the image needs two venvs. With ALPACA_TOOLSETS below it lists 25 tools;
// the allowlist keeps the stock/option market-data and news ones (docs and crypto tools left out).
// Fail-closed: a tool must be in ALPACA_ALLOWED_TOOLS AND not match ALPACA_DENY (second gate).
export const ALPACA_ALLOWED_TOOLS: ReadonlySet<string> = new Set([
  "get_stock_bars", "get_stock_quotes", "get_stock_trades",
  "get_stock_latest_bar", "get_stock_latest_quote", "get_stock_latest_trade", "get_stock_snapshot",
  "get_option_bars", "get_option_trades", "get_option_latest_trade", "get_option_latest_quote",
  "get_option_snapshot", "get_option_chain", "get_option_exchange_codes",
  "get_news",
  "get_market_movers", "get_most_active_stocks",
]);
export const ALPACA_DENY = /order|position|account|watchlist|cancel|close|exercise|replace|locate|portfolio/i;

const CONNECT_TIMEOUT_MS = 60_000;

export function buildServerSpecs(keys: McpKeys): McpServerSpec[] {
  const specs: McpServerSpec[] = [];
  const fred = keys.fred_api_key ?? "";
  specs.push({
    name: "openbb",
    command: "openbb-mcp",
    args: [...OPENBB_ARGS],
    env: fred ? { [OPENBB_FRED_ENV]: fred } : {},
    allow: (n) => OPENBB_ALLOWED_TOOLS.has(n) && (fred !== "" || !OPENBB_FRED_KEY_TOOLS.has(n)),
  });
  if (keys.alpaca_api_key && keys.alpaca_secret_key) {
    specs.push({
      name: "alpaca",
      command: "alpaca-mcp-server",
      args: [],
      env: {
        ALPACA_API_KEY: keys.alpaca_api_key,
        ALPACA_SECRET_KEY: keys.alpaca_secret_key,
        ALPACA_PAPER_TRADE: "true",
        ALPACA_TOOLSETS: "stock-data,options-data,news",
        ALPACA_MCP_USER_AGENT: "",
      },
      allow: (n) => ALPACA_ALLOWED_TOOLS.has(n) && !ALPACA_DENY.test(n),
    });
  }
  return specs;
}

async function listAllTools(client: Client): Promise<Tool[]> {
  const tools: Tool[] = [];
  let cursor: string | undefined;
  do {
    const page = await client.listTools(cursor ? { cursor } : undefined);
    tools.push(...page.tools);
    cursor = page.nextCursor;
  } while (cursor);
  return tools;
}

function resultText(result: unknown): string {
  const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
  return content.map((c) => (c.type === "text" ? (c.text ?? "") : `[${c.type} 内容已省略]`)).join("\n");
}

function toPiTool(spec: McpServerSpec, client: Client, tool: Tool, log: ToolLog): ToolDefinition {
  const name = `${spec.name}_${tool.name}`;
  return wrapTool(log, {
    name,
    label: name,
    description: tool.description ?? tool.title ?? tool.name,
    parameters: Type.Unsafe(tool.inputSchema),
    async run(params, signal) {
      const result = await client.callTool({ name: tool.name, arguments: params ?? {} }, undefined, { signal });
      const text = resultText(result);
      if (result.isError) throw new Error(text || `${name} 调用失败`);
      return text;
    },
  });
}

type Opened = { client: Client; tools: ToolDefinition[] } | { warning: string };

async function openServer(spec: McpServerSpec, log: ToolLog): Promise<Opened> {
  const client = new Client({ name: "bajiao-agent", version: "0.1.0" });
  const transport = new StdioClientTransport({
    command: spec.command,
    args: spec.args,
    // Only the SDK's minimal default env (HOME, PATH, ...) plus the spec's keys reach the server.
    env: { ...getDefaultEnvironment(), ...spec.env },
    stderr: "inherit",
  });
  try {
    await client.connect(transport, { timeout: CONNECT_TIMEOUT_MS });
    const listed = await listAllTools(client);
    return { client, tools: listed.filter((t) => spec.allow(t.name)).map((t) => toPiTool(spec, client, t, log)) };
  } catch (err) {
    await client.close().catch(() => {});
    return { warning: `MCP 服务器 ${spec.name} 启动失败: ${err instanceof Error ? err.message : String(err)}` };
  }
}

/** Starts every server in parallel; a server that fails to start is skipped with a warning. */
export async function openMcpTools(
  specs: McpServerSpec[],
  log: ToolLog,
): Promise<{ tools: ToolDefinition[]; warnings: string[]; close: () => Promise<void> }> {
  const opened = await Promise.all(specs.map((spec) => openServer(spec, log)));
  const clients: Client[] = [];
  const tools: ToolDefinition[] = [];
  const warnings: string[] = [];
  for (const o of opened) {
    if ("warning" in o) warnings.push(o.warning);
    else { clients.push(o.client); tools.push(...o.tools); }
  }
  return {
    tools,
    warnings,
    close: async () => {
      await Promise.all(clients.map((c) => c.close().catch(() => {})));
    },
  };
}

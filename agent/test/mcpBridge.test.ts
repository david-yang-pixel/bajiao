import { describe, it, expect, afterEach } from "vitest";
import { fileURLToPath } from "node:url";
import { ToolLog } from "../src/toolLog.js";
import {
  ALPACA_ALLOWED_TOOLS, ALPACA_DENY, buildServerSpecs, openMcpTools, OPENBB_ALLOWED_TOOLS, OPENBB_ARGS, OPENBB_FRED_ENV, OPENBB_FRED_KEY_TOOLS,
  type McpServerSpec,
} from "../src/mcpBridge.js";

const FIXTURE = fileURLToPath(new URL("./fixtures/fakeMcpServer.ts", import.meta.url));
const TSX = fileURLToPath(new URL("../node_modules/tsx/dist/loader.mjs", import.meta.url));
const KEYS = { alpaca_api_key: "ak", alpaca_secret_key: "sk", fred_api_key: "fk" };

function alpacaSpec(): McpServerSpec {
  const spec = buildServerSpecs(KEYS).find((s) => s.name === "alpaca")!;
  return { ...spec, command: process.execPath, args: ["--import", TSX, FIXTURE] };
}

function alive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

const closers: Array<() => Promise<void>> = [];
afterEach(async () => { while (closers.length) await closers.pop()!(); });

describe("buildServerSpecs (alpaca)", () => {
  it("configures alpaca read-only toolsets with paper trading", () => {
    const spec = buildServerSpecs(KEYS).find((s) => s.name === "alpaca")!;
    expect(spec.command).toBe("alpaca-mcp-server");
    expect(spec.env).toEqual({
      ALPACA_API_KEY: "ak",
      ALPACA_SECRET_KEY: "sk",
      ALPACA_PAPER_TRADE: "true",
      ALPACA_TOOLSETS: "stock-data,options-data,news",
      ALPACA_MCP_USER_AGENT: "",
    });
  });
  it("allows only the explicit read-only allowlist", () => {
    const { allow } = buildServerSpecs(KEYS).find((s) => s.name === "alpaca")!;
    expect([...ALPACA_ALLOWED_TOOLS].sort()).toEqual([
      "get_market_movers", "get_most_active_stocks", "get_news",
      "get_option_bars", "get_option_chain", "get_option_exchange_codes", "get_option_latest_quote",
      "get_option_latest_trade", "get_option_snapshot", "get_option_trades",
      "get_stock_bars", "get_stock_latest_bar", "get_stock_latest_quote", "get_stock_latest_trade",
      "get_stock_quotes", "get_stock_snapshot", "get_stock_trades",
    ]);
    for (const n of ALPACA_ALLOWED_TOOLS) expect(allow(n), n).toBe(true);
    // Every non-allowlisted tool the real server can expose (with or without ALPACA_TOOLSETS) is rejected.
    for (const n of ["place_stock_order", "place_option_order", "get_all_positions", "get_account_info", "create_watchlist",
      "cancel_all_orders", "close_position", "exercise_options_position", "replace_order_by_id", "GetOrders",
      "create_locate", "get_locates", "get_locate", "get_locate_quotes", "get_portfolio_history", "update_account_config",
      "fetch_alpaca_doc", "search_alpaca_docs", "get_crypto_bars", "get_asset", "some_future_tool"]) {
      expect(allow(n), n).toBe(false);
    }
  });
  it("allowlist contains nothing the deny regex would block", () => {
    for (const n of ALPACA_ALLOWED_TOOLS) expect(ALPACA_DENY.test(n), n).toBe(false);
    for (const n of ["create_locate", "get_portfolio_history", "place_stock_order", "get_all_positions"]) {
      expect(ALPACA_DENY.test(n), n).toBe(true);
    }
  });
  it("skips alpaca when keys are missing", () => {
    expect(buildServerSpecs({ alpaca_api_key: "", alpaca_secret_key: "sk", fred_api_key: "" }).map((s) => s.name)).toEqual(["openbb"]);
    expect(buildServerSpecs({ alpaca_api_key: "ak", alpaca_secret_key: "", fred_api_key: "" }).map((s) => s.name)).toEqual(["openbb"]);
  });
});

describe("buildServerSpecs (openbb)", () => {
  const WHITELIST = [
    "derivatives_options_chains", "economy_calendar", "equity_calendar_earnings", "equity_estimates_consensus",
    "equity_estimates_price_target", "equity_fundamental_filings", "equity_fundamental_historical_eps",
    "equity_ownership_insider_trading", "index_price_historical", "yfinance_company_calendar",
    "yfinance_estimates_recommendations",
  ];
  it("runs openbb-mcp over stdio restricted to the needed categories", () => {
    const spec = buildServerSpecs(KEYS).find((s) => s.name === "openbb")!;
    expect(spec.command).toBe("openbb-mcp");
    expect(spec.args).toEqual(["--transport", "stdio", "--allowed-categories", "equity,derivatives,index,economy,yfinance"]);
    expect(spec.args).toEqual([...OPENBB_ARGS]);
    expect(spec.env).toEqual({ FRED_API_KEY: "fk" });
    expect(OPENBB_FRED_ENV).toBe("FRED_API_KEY");
  });
  it("allows exactly the whitelisted tools", () => {
    expect([...OPENBB_ALLOWED_TOOLS].sort()).toEqual(WHITELIST);
    const { allow } = buildServerSpecs(KEYS).find((s) => s.name === "openbb")!;
    for (const n of WHITELIST) expect(allow(n), n).toBe(true);
    for (const n of ["run_pipeline", "install_skill", "read_resource", "get_prompt", "equity_price_historical",
      "equity_price_quote", "economy_fred_series", "sec_htm_file", "yfinance_tv_widget"]) expect(allow(n), n).toBe(false);
  });
  it("still starts without a FRED key, without the key env and dropping only key-requiring FRED tools", () => {
    const spec = buildServerSpecs({ alpaca_api_key: "", alpaca_secret_key: "", fred_api_key: "" })[0];
    expect(spec.name).toBe("openbb");
    expect(spec.env).toEqual({});
    for (const n of OPENBB_FRED_KEY_TOOLS) expect(spec.allow(n), n).toBe(false);
    // The FRED release calendar (economy_calendar, provider=fred) scrapes fred.stlouisfed.org and needs no key.
    expect(spec.allow("economy_calendar")).toBe(true);
  });
});

describe("openMcpTools", () => {
  it("exposes allowed tools with server prefix and filters place_order / create_locate", async () => {
    const log = new ToolLog();
    const r = await openMcpTools([alpacaSpec()], log);
    closers.push(r.close);
    expect(r.warnings).toEqual([]);
    expect(r.tools.map((t) => t.name)).toEqual(["alpaca_get_news"]);
    const t = r.tools[0];
    expect(t.description).toBe("Get news for a symbol");
    expect((t.parameters as any).properties.symbol.type).toBe("string");
    expect((t.parameters as any).required).toEqual(["symbol"]);
  });

  it("calls the MCP tool, joins text content and records in ToolLog", async () => {
    const log = new ToolLog();
    const r = await openMcpTools([alpacaSpec()], log);
    closers.push(r.close);
    const res = await r.tools[0].execute("x", { symbol: "AAPL" }, undefined, undefined, {} as any);
    const text = (res.content[0] as any).text as string;
    expect(text).toMatch(/^\[call_id=c1\]\nnews for AAPL https:\/\/example\.com\/news\/AAPL\npid=\d+$/);
    expect(log.entries()[0]).toMatchObject({
      tool: "alpaca_get_news", args: { symbol: "AAPL" }, is_error: false, urls: ["https://example.com/news/AAPL"],
    });
  });

  it("turns MCP isError results into thrown errors that are logged", async () => {
    const log = new ToolLog();
    const r = await openMcpTools([alpacaSpec()], log);
    closers.push(r.close);
    await expect(r.tools[0].execute("x", { symbol: "FAIL" }, undefined, undefined, {} as any)).rejects.toThrow("upstream error for FAIL");
    expect(log.entries()[0]).toMatchObject({ tool: "alpaca_get_news", is_error: true });
  });

  it("skips a server that fails to start and keeps the others", async () => {
    const log = new ToolLog();
    const broken: McpServerSpec = { name: "openbb", command: "/nonexistent/openbb-mcp-xyz", args: [], env: {}, allow: () => true };
    const r = await openMcpTools([broken, alpacaSpec()], log);
    closers.push(r.close);
    expect(r.warnings.length).toBe(1);
    expect(r.warnings[0]).toContain("openbb");
    expect(r.tools.map((t) => t.name)).toEqual(["alpaca_get_news"]);
  });

  it("close() terminates the server processes", async () => {
    const log = new ToolLog();
    const r = await openMcpTools([alpacaSpec()], log);
    const res = await r.tools[0].execute("x", { symbol: "MSFT" }, undefined, undefined, {} as any);
    const pid = Number(/pid=(\d+)/.exec((res.content[0] as any).text)![1]);
    expect(alive(pid)).toBe(true);
    await r.close();
    for (let i = 0; i < 50 && alive(pid); i++) await new Promise((ok) => setTimeout(ok, 100));
    expect(alive(pid)).toBe(false);
  });
});

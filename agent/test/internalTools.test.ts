import { describe, it, expect } from "vitest";
import { createInternalTools } from "../src/internalTools.js";
import { ToolLog } from "../src/toolLog.js";
import { CONFIG } from "../src/config.js";

function mk(status: number, body: unknown, calls: any[] = []) {
  const f = (async (url: string, init: any) => {
    calls.push({ url, init });
    return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
  }) as unknown as typeof fetch;
  const log = new ToolLog();
  const tools = createInternalTools(log, f, () => "tok123");
  const get = (n: string) => tools.find((t) => t.name === n)!;
  return { get, log };
}
const exec = (t: any, p: any) => t.execute("id", p, undefined, undefined, {} as any);

describe("internal tools", () => {
  it("exposes four tools", () => {
    const { get } = mk(200, {});
    for (const n of ["get_factors", "get_token_quote", "get_crypto_context", "get_market_context"]) expect(get(n)).toBeTruthy();
  });
  it("posts symbol with bearer token", async () => {
    const calls: any[] = [];
    const { get } = mk(200, { rsi: 55 }, calls);
    const r = await exec(get("get_factors"), { symbol: "NVDA" });
    expect(calls[0].url).toBe(`${CONFIG.mainUrl}/internal/tools/get_factors`);
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.headers.Authorization).toBe("Bearer tok123");
    expect(JSON.parse(calls[0].init.body)).toEqual({ symbol: "NVDA" });
    expect(r.content[0].text).toContain("[call_id=c1]");
    expect(r.content[0].text).toContain('"rsi":55');
  });
  it("describes get_market_context as what it returns (SPY/QQQ + macro events, no sector ETFs)", () => {
    const d = mk(200, {}).get("get_market_context").description;
    expect(d).toContain("SPY/QQQ");
    expect(d).toContain("宏观");
    expect(d).not.toContain("板块");
  });
  it("context tools post empty object", async () => {
    const calls: any[] = [];
    const { get } = mk(200, { ok: 1 }, calls);
    await exec(get("get_market_context"), {});
    expect(calls[0].url).toMatch(/\/internal\/tools\/get_market_context$/);
    expect(JSON.parse(calls[0].init.body)).toEqual({});
  });
  it("503 throws detail and logs error", async () => {
    const { get, log } = mk(503, { detail: "数据源不可用" });
    await expect(exec(get("get_token_quote"), { symbol: "X" })).rejects.toThrow("数据源不可用");
    expect(log.entries()[0].is_error).toBe(true);
  });
  it("non-json error falls back to status text", async () => {
    const { get } = mk(404, "nope");
    await expect(exec(get("get_factors"), { symbol: "X" })).rejects.toThrow(/Not Found|404/);
  });
});

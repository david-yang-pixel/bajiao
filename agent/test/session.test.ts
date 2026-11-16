import { describe, it, expect, vi, afterEach } from "vitest";
import { createAgentSession, ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CONFIG } from "../src/config.js";
import { ToolLog, wrapTool } from "../src/toolLog.js";
import { agentState, fetchAgentConfig, runAnalysis, type AnalyzeRequest } from "../src/session.js";

CONFIG.dataDir = mkdtempSync(join(tmpdir(), "agent-session-"));

const REQ: AnalyzeRequest = {
  symbol: "NVDA",
  token_symbol: "NVDAB",
  purpose: "pick",
  context: { as_of: "2026-11-13", reference_price_usd: 182.4, position: null },
};
const KEYS = { alpaca_api_key: "ak", alpaca_secret_key: "sk", fred_api_key: "fk", model: "gpt-test" };
const MODEL = { provider: "openai-codex", id: "gpt-test" };
const PICK = {
  symbol: "NVDA", decision: "selected", current_price: 182.4, target_price: 194, stop_price: 176.8,
  rationale: "放量突破 20 日高点，分析师上调目标价，短线动能延续", invalidation: "收盘跌破 176.8",
  risks: ["大盘回撤"], factor_highlights: ["量比 2.1"],
  citations: [{ call_id: "c1" }, { call_id: "c2", url: "https://news.example.com/nvda" }],
};

type Emit = (e: any) => void;
type Script = (tools: ToolDefinition[], emit: Emit, session: any) => Promise<void>;

function fakeRuntime(over: Partial<Record<string, any>> = {}) {
  return {
    hasConfiguredAuth: vi.fn(() => true),
    getModel: vi.fn((provider: string, id: string) => ({ provider, id })),
    getAvailable: vi.fn(async () => [{ provider: "openai-codex", id: "gpt-first" }, { provider: "openai-codex", id: "gpt-second" }]),
    ...over,
  } as unknown as ModelRuntime;
}

function fakeCreate(script: Script) {
  const state: { opts: any; session: any } = { opts: undefined, session: undefined };
  let resolvePrompted!: () => void;
  const prompted = new Promise<void>((r) => (resolvePrompted = r));
  const create = vi.fn(async (opts: any) => {
    state.opts = opts;
    const listeners: Emit[] = [];
    const session: any = {
      messages: [] as any[],
      subscribe: (l: Emit) => { listeners.push(l); return () => {}; },
      prompt: vi.fn(async (_text: string) => {
        resolvePrompted();
        await script(opts.customTools, (e) => listeners.forEach((l) => l(e)), session);
      }),
      abort: vi.fn(async () => {}),
      dispose: vi.fn(),
    };
    state.session = session;
    return { session, extensionsResult: {} };
  });
  return { create: create as unknown as typeof createAgentSession, state, prompted };
}

let seq = 0;
async function call(tools: ToolDefinition[], emit: Emit, name: string, args: unknown) {
  const tool = tools.find((t) => t.name === name)!;
  const id = `tc${++seq}`;
  emit({ type: "tool_execution_start", toolCallId: id, toolName: name, args });
  try {
    const result = await tool.execute(id, args as any, undefined, undefined, {} as any);
    emit({ type: "tool_execution_end", toolCallId: id, toolName: name, result, isError: false });
    return result;
  } catch (err) {
    // pi turns a thrown tool error into an error tool result; the run continues.
    emit({ type: "tool_execution_end", toolCallId: id, toolName: name, result: { content: [{ type: "text", text: String(err) }] }, isError: true });
    return undefined;
  }
}

function fakeMcp() {
  const close = vi.fn(async () => {});
  const open = vi.fn(async (_specs: any, log: ToolLog) => ({
    tools: [
      wrapTool(log, {
        name: "alpaca_get_news", label: "news", description: "news",
        parameters: Type.Object({ symbol: Type.String() }),
        run: async () => "NVDA 新闻 https://news.example.com/nvda",
      }),
    ],
    warnings: [],
    close,
  }));
  return { open: open as any, close };
}

const fetchCalls: Array<{ url: string; init: any }> = [];
const fakeFetch = (async (url: string, init: any) => {
  fetchCalls.push({ url, init });
  return new Response(JSON.stringify({ price: 182.4, atr14: 5.2 }), { status: 200 });
}) as unknown as typeof fetch;

function deps(create: typeof createAgentSession, extra: Record<string, any> = {}) {
  const mcp = fakeMcp();
  return {
    mcp,
    deps: {
      createSession: create,
      modelRuntime: fakeRuntime(),
      fetchKeys: vi.fn(async () => ({ ...KEYS })),
      openMcp: mcp.open,
      fetchImpl: fakeFetch,
      tokenProvider: () => "tok",
      logger: () => {},
      ...extra,
    },
  };
}

const happy: Script = async (tools, emit) => {
  await call(tools, emit, "get_factors", { symbol: "NVDA" });
  await call(tools, emit, "alpaca_get_news", { symbol: "NVDA" });
  await call(tools, emit, "submit_pick", PICK);
};

afterEach(() => {
  vi.useRealTimers();
  agentState.lastAuthState = "unknown";
  agentState.lastModel = null;
});

describe("runAnalysis", () => {
  it("returns ok with decision, pick and tool_log when the agent submits", async () => {
    const { create, state } = fakeCreate(happy);
    const { deps: d, mcp } = deps(create);
    const res = await runAnalysis(REQ, d);
    expect(res.status).toBe("ok");
    expect(res.error).toBeNull();
    expect(res.decision).toBe("selected");
    expect(res.pick).toMatchObject({ symbol: "NVDA", current_price: 182.4 });
    // two wrapped tool calls; the subscribe fallback must not duplicate them, submit_pick is not logged
    expect(res.tool_log.map((e) => [e.call_id, e.tool])).toEqual([["c1", "get_factors"], ["c2", "alpaca_get_news"]]);
    expect(res.tool_log[1].urls).toEqual(["https://news.example.com/nvda"]);
    expect(res.usage.tool_calls).toBe(2);
    expect(typeof res.usage.duration_ms).toBe("number");
    expect(mcp.close).toHaveBeenCalledOnce();
    expect(state.session.dispose).toHaveBeenCalled();
    expect(fetchCalls.at(-1)!.init.headers.Authorization).toBe("Bearer tok");
    expect(agentState.lastAuthState).toBe("valid");
    expect(agentState.lastModel).toBe("openai-codex/gpt-test");
  });

  it("creates the pi session with only custom tools, in-memory managers and the overridden system prompt", async () => {
    const { create, state } = fakeCreate(happy);
    const { deps: d } = deps(create);
    await runAnalysis(REQ, d);
    const o = state.opts;
    expect(o.noTools).toBe("builtin");
    expect(o.sessionManager).toBeInstanceOf(SessionManager);
    expect(o.model).toEqual(MODEL);
    expect(d.modelRuntime.getModel).toHaveBeenCalledWith("openai-codex", "gpt-test");
    expect(o.modelRuntime).toBe(d.modelRuntime);
    expect(o.customTools.map((t: ToolDefinition) => t.name).sort()).toEqual([
      "alpaca_get_news", "get_crypto_context", "get_factors", "get_market_context", "get_token_quote", "submit_pick",
    ]);
    const sp: string = o.resourceLoader.getSystemPrompt();
    expect(sp).toContain("短线");
    expect(sp).toContain("NVDA");
    expect(sp).toContain("NVDAB");
    expect(sp).toContain("182.4");
    expect(sp).toContain("get_factors");
    expect(sp).toContain("submit_pick");
    expect(sp).toContain("call_id");
    expect(sp).toContain("不可用");
    expect(state.session.prompt).toHaveBeenCalledOnce();
  });

  it("picks the first available openai-codex model when keys.model is empty", async () => {
    const { create, state } = fakeCreate(happy);
    const { deps: d } = deps(create, { fetchKeys: async () => ({ ...KEYS, model: "" }) });
    const res = await runAnalysis(REQ, d);
    expect(res.status).toBe("ok");
    expect(d.modelRuntime.getAvailable).toHaveBeenCalledWith("openai-codex");
    expect(state.opts.model).toEqual({ provider: "openai-codex", id: "gpt-first" });
    expect(agentState.lastModel).toBe("openai-codex/gpt-first");
  });

  it("returns no_submission when the agent never calls submit_pick", async () => {
    const { create } = fakeCreate(async (tools, emit) => {
      await call(tools, emit, "get_factors", { symbol: "NVDA" });
    });
    const { deps: d, mcp } = deps(create);
    const res = await runAnalysis(REQ, d);
    expect(res.status).toBe("error");
    expect(res.error?.code).toBe("no_submission");
    expect(res.decision).toBeNull();
    expect(res.pick).toBeNull();
    expect(res.tool_log).toHaveLength(1);
    expect(mcp.close).toHaveBeenCalledOnce();
  });

  it("times out after sessionTimeoutMs, aborts the session and closes MCP", async () => {
    vi.useFakeTimers();
    const { create, state, prompted } = fakeCreate(() => new Promise<void>(() => {}));
    const { deps: d, mcp } = deps(create);
    const pending = runAnalysis(REQ, d);
    await prompted;
    await vi.advanceTimersByTimeAsync(300_000);
    const res = await pending;
    expect(res.status).toBe("error");
    expect(res.error?.code).toBe("timeout");
    expect(state.session.abort).toHaveBeenCalled();
    expect(mcp.close).toHaveBeenCalledOnce();
  });

  it("counts MCP startup against the session deadline (deadline starts at request start)", async () => {
    vi.useFakeTimers();
    const { create, prompted } = fakeCreate(() => new Promise<void>(() => {}));
    const { deps: d, mcp } = deps(create);
    const realOpen = mcp.open.getMockImplementation()!;
    mcp.open.mockImplementation(async (specs: any, log: ToolLog) => {
      await new Promise((r) => setTimeout(r, 200_000)); // slow MCP server start-up
      return realOpen(specs, log);
    });
    let done = false;
    const pending = runAnalysis(REQ, d).finally(() => (done = true));
    await vi.advanceTimersByTimeAsync(200_000);
    await prompted;
    await vi.advanceTimersByTimeAsync(100_000); // 300 s since the request started
    expect(done).toBe(true);
    if (!done) await vi.advanceTimersByTimeAsync(400_000);
    const res = await pending;
    expect(res.error?.code).toBe("timeout");
    expect(res.usage.duration_ms).toBeLessThanOrEqual(CONFIG.sessionTimeoutMs);
  });

  it("does not let a hanging MCP shutdown hold the response past the deadline", async () => {
    vi.useFakeTimers();
    const { create } = fakeCreate(happy);
    const { deps: d, mcp } = deps(create);
    mcp.close.mockImplementation(() => new Promise<void>(() => {}));
    let done = false;
    const pending = runAnalysis(REQ, d).finally(() => (done = true));
    await vi.advanceTimersByTimeAsync(CONFIG.sessionTimeoutMs);
    expect(done).toBe(true);
    if (!done) return;
    expect((await pending).status).toBe("ok");
    expect(mcp.close).toHaveBeenCalledOnce();
  });

  it("returns tool_limit when the tool budget is exhausted without a submission", async () => {
    const { create } = fakeCreate(async (tools, emit) => {
      for (let i = 0; i < CONFIG.maxToolCalls + 3; i++) await call(tools, emit, "get_factors", { symbol: "NVDA" });
    });
    const { deps: d } = deps(create);
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("tool_limit");
    expect(res.tool_log).toHaveLength(CONFIG.maxToolCalls);
  });

  it("returns auth_missing without starting MCP or a session when no ChatGPT credential is configured", async () => {
    const { create } = fakeCreate(happy);
    const { deps: d, mcp } = deps(create, { modelRuntime: fakeRuntime({ hasConfiguredAuth: vi.fn(() => false) }) });
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("auth_missing");
    expect(create).not.toHaveBeenCalled();
    expect(mcp.open).not.toHaveBeenCalled();
    expect(agentState.lastAuthState).toBe("missing");
  });

  it("maps an authentication failure to auth_expired and a later success back to valid", async () => {
    const { create } = fakeCreate(async () => {
      throw new Error('Authentication failed for "openai-codex". Run \'/login openai-codex\' to re-authenticate.');
    });
    const { deps: d, mcp } = deps(create);
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("auth_expired");
    expect(agentState.lastAuthState).toBe("expired");
    expect(mcp.close).toHaveBeenCalledOnce();

    const ok = await runAnalysis(REQ, deps(fakeCreate(happy).create).deps);
    expect(ok.status).toBe("ok");
    expect(agentState.lastAuthState).toBe("valid");
  });

  it("maps a model error ending the run to model_error", async () => {
    const { create } = fakeCreate(async (_tools, _emit, session) => {
      session.messages.push({ role: "assistant", stopReason: "error", errorMessage: "rate limit exceeded (429)", content: [] });
    });
    const { deps: d } = deps(create);
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("model_error");
    expect(res.error?.message).toContain("429");
  });

  it("classifies auth-like vs other model error messages", async () => {
    const run = async (errorMessage: string) => {
      const { create } = fakeCreate(async (_tools, _emit, session) => {
        session.messages.push({ role: "assistant", stopReason: "error", errorMessage, content: [] });
      });
      return (await runAnalysis(REQ, deps(create).deps)).error?.code;
    };
    expect(await run("This model's maximum context length is 272000 tokens")).toBe("model_error");
    expect(await run("max_tokens must be positive")).toBe("model_error");
    expect(await run("HTTP 401 Unauthorized")).toBe("auth_expired");
    expect(await run("refresh token is invalid")).toBe("auth_expired");
    expect(await run("Please login again")).toBe("auth_expired");
  });

  it("returns model_error when keys.model names an unknown model", async () => {
    const { create } = fakeCreate(happy);
    const { deps: d, mcp } = deps(create, { modelRuntime: fakeRuntime({ getModel: vi.fn(() => undefined) }) });
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("model_error");
    expect(res.error?.message).toContain("gpt-test");
    expect(mcp.open).not.toHaveBeenCalled();
  });

  it("returns auth_expired when credentials exist but no openai-codex model is available", async () => {
    const { create } = fakeCreate(happy);
    const { deps: d } = deps(create, {
      fetchKeys: async () => ({ ...KEYS, model: "" }),
      modelRuntime: fakeRuntime({ getAvailable: vi.fn(async () => []) }),
    });
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("auth_expired");
    expect(agentState.lastAuthState).toBe("expired");
  });

  it("reports a failure to fetch agent config without opening MCP", async () => {
    const { create } = fakeCreate(happy);
    const { deps: d, mcp } = deps(create, { fetchKeys: async () => { throw new Error("HTTP 503"); } });
    const res = await runAnalysis(REQ, d);
    expect(res.status).toBe("error");
    expect(res.error?.code).toBe("model_error");
    expect(res.error?.message).toContain("HTTP 503");
    expect(mcp.open).not.toHaveBeenCalled();
  });

  it("logs tool executions not recorded by wrapped tools as a fallback", async () => {
    const { create } = fakeCreate(async (tools, emit) => {
      await call(tools, emit, "get_factors", { symbol: "NVDA" });
      emit({ type: "tool_execution_start", toolCallId: "x1", toolName: "made_up_tool", args: { a: 1 } });
      emit({ type: "tool_execution_end", toolCallId: "x1", toolName: "made_up_tool", result: { content: [{ type: "text", text: "Tool made_up_tool not found" }] }, isError: true });
    });
    const { deps: d } = deps(create);
    const res = await runAnalysis(REQ, d);
    expect(res.tool_log.map((e) => e.tool)).toEqual(["get_factors", "made_up_tool"]);
    expect(res.tool_log[1]).toMatchObject({ call_id: "c2", args: { a: 1 }, is_error: true, summary: "Tool made_up_tool not found" });
  });

  it("tells the model about position context and unavailable sources for position reviews", async () => {
    const { create, state } = fakeCreate(happy);
    const mcp = fakeMcp();
    mcp.open.mockImplementation(async () => ({ tools: [], warnings: ["MCP 服务器 alpaca 启动失败: boom"], close: mcp.close }));
    const { deps: d } = deps(create, { openMcp: mcp.open });
    await runAnalysis({ ...REQ, purpose: "position_review", context: { ...REQ.context, position: { buy_price_usdt: 170.5, quantity: 2 } } }, d);
    const sp: string = state.opts.resourceLoader.getSystemPrompt();
    expect(sp).toContain("170.5");
    expect(sp).toContain("hold");
    expect(sp).toContain("alpaca 启动失败");
  });

  it("wires a real pi session with only the custom tools active and the skill as system prompt", async () => {
    const runtime = await ModelRuntime.create({ authPath: join(CONFIG.dataDir, "no-auth.json") });
    runtime.hasConfiguredAuth = () => true;
    let active: string[] = [];
    let systemPrompt = "";
    const realCreate = (async (opts: any) => {
      const r = await createAgentSession(opts);
      active = r.session.getActiveToolNames();
      systemPrompt = r.session.systemPrompt;
      r.session.prompt = async () => {}; // never reach the model
      return r;
    }) as typeof createAgentSession;
    const { deps: d } = deps(realCreate, { modelRuntime: runtime, fetchKeys: async () => ({ ...KEYS, model: "gpt-5.6-luna" }) });
    const res = await runAnalysis(REQ, d);
    expect(res.error?.code).toBe("no_submission");
    expect(active.sort()).toEqual([
      "alpaca_get_news", "get_crypto_context", "get_factors", "get_market_context", "get_token_quote", "submit_pick",
    ]);
    expect(systemPrompt).toContain("短线");
    expect(systemPrompt).toContain("NVDA");
  });
});

describe("fetchAgentConfig", () => {
  it("GETs the internal agent-config endpoint with the bearer token", async () => {
    const calls: any[] = [];
    const f = (async (url: string, init: any) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(KEYS), { status: 200 });
    }) as unknown as typeof fetch;
    expect(await fetchAgentConfig(f, () => "tok9")).toEqual(KEYS);
    expect(calls[0].url).toBe(`${CONFIG.mainUrl}/internal/agent-config`);
    expect(calls[0].init.headers.Authorization).toBe("Bearer tok9");
  });
  it("throws on a non-200 response", async () => {
    const f = (async () => new Response("no", { status: 401 })) as unknown as typeof fetch;
    await expect(fetchAgentConfig(f, () => "t")).rejects.toThrow(/401/);
  });
});

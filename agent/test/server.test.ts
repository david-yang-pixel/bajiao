import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CONFIG } from "../src/config.js";
import { agentState, type AnalyzeRequest, type AnalyzeResponse } from "../src/session.js";
import { createAgentServer } from "../src/server.js";

const TOKEN = "internal-secret";
const CODEX_AUTH = JSON.stringify({ "openai-codex": { type: "oauth", access: "a", refresh: "r", expires: 0 } });
const REQ: AnalyzeRequest = {
  symbol: "NVDA",
  token_symbol: "NVDAB",
  purpose: "pick",
  context: { as_of: "2026-11-13", reference_price_usd: 182.4, position: null },
};
const OK: AnalyzeResponse = {
  status: "ok", decision: "rejected", pick: { symbol: "NVDA" } as any, tool_log: [],
  usage: { tool_calls: 1, duration_ms: 5 }, error: null,
};

let server: Server;
let base: string;
let run: ReturnType<typeof vi.fn>;
let createRuntime: ReturnType<typeof vi.fn>;

async function start(runImpl: (...a: any[]) => Promise<AnalyzeResponse>) {
  run = vi.fn(runImpl);
  createRuntime = vi.fn(async () => ({ fake: "runtime" }));
  server = createAgentServer({ runAnalysis: run as any, createModelRuntime: createRuntime as any, logger: () => {} });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

const analyze = (body: unknown, token: string | null = TOKEN) =>
  fetch(`${base}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token === null ? {} : { Authorization: `Bearer ${token}` }) },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

beforeEach(() => {
  CONFIG.dataDir = mkdtempSync(join(tmpdir(), "agent-server-"));
  writeFileSync(join(CONFIG.dataDir, "internal_token"), `${TOKEN}\n`);
  agentState.lastAuthState = "unknown";
  agentState.lastModel = null;
});
afterEach(async () => {
  await new Promise<void>((r) => server.close(() => r()));
  rmSync(CONFIG.dataDir, { recursive: true, force: true });
});

describe("POST /analyze", () => {
  it("rejects a missing bearer token with 401", async () => {
    await start(async () => OK);
    const res = await analyze(REQ, null);
    expect(res.status).toBe(401);
    expect(run).not.toHaveBeenCalled();
  });

  it("rejects a wrong bearer token with 401", async () => {
    await start(async () => OK);
    expect((await analyze(REQ, "nope")).status).toBe(401);
    expect(run).not.toHaveBeenCalled();
  });

  it("rejects malformed bodies with 400 bad_request", async () => {
    await start(async () => OK);
    const bad = [
      "{not json",
      {},
      { ...REQ, symbol: "" },
      { ...REQ, symbol: "NV DA; rm" },
      { ...REQ, purpose: "trade" },
      { ...REQ, context: null },
      { ...REQ, context: { ...REQ.context, reference_price_usd: "182" } },
      { ...REQ, context: { ...REQ.context, position: { buy_price_usdt: "x", quantity: 1 } } },
      { ...REQ, token_symbol: 5 },
    ];
    for (const b of bad) {
      const res = await analyze(b);
      expect(res.status, JSON.stringify(b)).toBe(400);
      const body = await res.json();
      expect(body.status).toBe("error");
      expect(body.error.code).toBe("bad_request");
    }
    expect(run).not.toHaveBeenCalled();
  });

  it("rejects bodies over 32 KB", async () => {
    await start(async () => OK);
    const res = await analyze({ ...REQ, pad: "x".repeat(33 * 1024) });
    expect(res.status).toBe(400);
    expect((await res.json()).error.code).toBe("bad_request");
  });

  it("returns auth_missing without creating a model runtime when no ChatGPT credential was uploaded", async () => {
    await start(async () => OK);
    // ModelRuntime.create writes "{}" to a missing authPath; an empty file is still "missing".
    writeFileSync(join(CONFIG.dataDir, "auth.json"), "{}");
    const res = await analyze(REQ);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.status).toBe("error");
    expect(body.error.code).toBe("auth_missing");
    expect(createRuntime).not.toHaveBeenCalled();
    expect(run).not.toHaveBeenCalled();
    expect(agentState.lastAuthState).toBe("missing");
  });

  it("runs the analysis with a fresh model runtime on the uploaded auth.json", async () => {
    await start(async () => OK);
    writeFileSync(join(CONFIG.dataDir, "auth.json"), CODEX_AUTH);
    const res = await analyze({ ...REQ, purpose: "position_review", context: { ...REQ.context, position: { buy_price_usdt: 180, quantity: null } } });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(OK);
    expect(createRuntime).toHaveBeenCalledWith({ authPath: `${CONFIG.dataDir}/auth.json` });
    const [req, deps] = run.mock.calls[0];
    expect(req).toEqual({ ...REQ, purpose: "position_review", context: { ...REQ.context, position: { buy_price_usdt: 180, quantity: null } } });
    expect(deps.modelRuntime).toEqual({ fake: "runtime" });
    expect(typeof deps.fetchKeys).toBe("function");
  });

  it("allows at most 3 concurrent analyses; the 4th gets 429 busy", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let entered = 0;
    await start(async () => { entered++; await gate; return OK; });
    writeFileSync(join(CONFIG.dataDir, "auth.json"), CODEX_AUTH);
    const first = [analyze(REQ), analyze(REQ), analyze(REQ)];
    await vi.waitFor(() => expect(entered).toBe(3));
    const fourth = await analyze(REQ);
    expect(fourth.status).toBe(429);
    expect(await fourth.json()).toEqual({ error: { code: "busy" } });
    release();
    for (const r of await Promise.all(first)) expect(r.status).toBe(200);
    // capacity is released afterwards
    expect((await analyze(REQ)).status).toBe(200);
  });
});

describe("GET /health", () => {
  it("reports auth missing when auth.json does not exist", async () => {
    await start(async () => OK);
    process.env.APP_REVISION = "rev123";
    const body = await (await fetch(`${base}/health`)).json();
    expect(body).toMatchObject({ status: "ok", auth: "missing", revision: "rev123" });
  });

  it("reports missing when auth.json has no openai-codex credential", async () => {
    await start(async () => OK);
    writeFileSync(join(CONFIG.dataDir, "auth.json"), "{}");
    expect((await (await fetch(`${base}/health`)).json()).auth).toBe("missing");
    writeFileSync(join(CONFIG.dataDir, "auth.json"), "not json");
    expect((await (await fetch(`${base}/health`)).json()).auth).toBe("missing");
  });

  it("reports valid when an openai-codex credential exists and expired after an auth failure", async () => {
    await start(async () => OK);
    writeFileSync(join(CONFIG.dataDir, "auth.json"), CODEX_AUTH);
    expect((await (await fetch(`${base}/health`)).json()).auth).toBe("valid");
    agentState.lastAuthState = "expired";
    agentState.lastModel = "openai-codex/gpt-first";
    const body = await (await fetch(`${base}/health`)).json();
    expect(body.auth).toBe("expired");
    expect(body.model).toBe("openai-codex/gpt-first");
  });

  it("returns 404 for unknown routes", async () => {
    await start(async () => OK);
    expect((await fetch(`${base}/nope`)).status).toBe(404);
  });
});

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { timingSafeEqual } from "node:crypto";
import { pathToFileURL } from "node:url";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { CONFIG, readToken } from "./config.js";
import { agentState, fetchAgentConfig, runAnalysis, type AnalyzeRequest, type AnalyzeResponse } from "./session.js";

const MAX_BODY_BYTES = 32 * 1024;
const MAX_CONCURRENT = 3;
const SYMBOL_RE = /^[A-Za-z0-9][A-Za-z0-9.\-]{0,29}$/;

export interface ServerDeps {
  runAnalysis?: typeof runAnalysis;
  createModelRuntime?: (options: { authPath: string }) => Promise<ModelRuntime>;
  logger?: (line: string) => void;
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Content-Length": Buffer.byteLength(text) });
  res.end(text);
}

function errorResponse(code: "bad_request" | "model_error" | "auth_missing", message: string): AnalyzeResponse {
  return { status: "error", decision: null, pick: null, tool_log: [], usage: { tool_calls: 0, duration_ms: 0 }, error: { code, message } };
}

function authorized(header: string | undefined): boolean | "no_token" {
  let expected: string;
  try {
    expected = readToken();
  } catch {
    return "no_token";
  }
  const match = /^Bearer (.+)$/.exec(header ?? "");
  if (!match || !expected) return false;
  const a = Buffer.from(match[1]);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** Reads the body; resolves null when it exceeds the limit (the rest is drained, not buffered). */
function readBody(req: IncomingMessage): Promise<string | null> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size <= MAX_BODY_BYTES) chunks.push(chunk);
    });
    req.on("end", () => resolve(size > MAX_BODY_BYTES ? null : Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const numOrNull = (v: unknown) => v === null || v === undefined || isNum(v);

/** Validates the POST /analyze body; returns the request or a Chinese error message. */
export function parseAnalyzeRequest(raw: unknown): AnalyzeRequest | string {
  if (!isObj(raw)) return "请求体必须是 JSON 对象";
  const { symbol, token_symbol, purpose, context } = raw;
  if (typeof symbol !== "string" || !SYMBOL_RE.test(symbol)) return "symbol 无效";
  if (typeof token_symbol !== "string" || !SYMBOL_RE.test(token_symbol)) return "token_symbol 无效";
  if (purpose !== "pick" && purpose !== "position_review") return "purpose 必须为 pick 或 position_review";
  if (!isObj(context)) return "context 必须是对象";
  const { as_of, reference_price_usd, position } = context;
  if (typeof as_of !== "string" || as_of.length === 0 || as_of.length > 40) return "context.as_of 无效";
  if (!numOrNull(reference_price_usd)) return "context.reference_price_usd 必须为数字或 null";
  if (position !== null && position !== undefined) {
    if (!isObj(position) || !numOrNull(position.buy_price_usdt) || !numOrNull(position.quantity)) {
      return "context.position 无效";
    }
  }
  return {
    symbol,
    token_symbol,
    purpose,
    context: {
      as_of,
      reference_price_usd: (reference_price_usd as number | null | undefined) ?? null,
      position: isObj(position)
        ? {
            buy_price_usdt: (position.buy_price_usdt as number | null | undefined) ?? null,
            quantity: (position.quantity as number | null | undefined) ?? null,
          }
        : null,
    },
  };
}

/** True when auth.json holds a credential for the ChatGPT (openai-codex) provider. */
function hasCredential(): boolean {
  try {
    const stored = JSON.parse(readFileSync(`${CONFIG.dataDir}/auth.json`, "utf8"));
    return isObj(stored) && isObj(stored[CONFIG.provider]);
  } catch {
    return false;
  }
}

function authMtimeMs(): number | null {
  try {
    return statSync(`${CONFIG.dataDir}/auth.json`).mtimeMs;
  } catch {
    return null;
  }
}

function authState(): "missing" | "expired" | "valid" {
  if (!hasCredential()) return "missing";
  if (agentState.lastAuthState !== "expired") return "valid";
  const mtime = authMtimeMs();
  if (agentState.authMtimeMs === undefined) {
    agentState.authMtimeMs = mtime; // state set without a recorded file version: pin the current one
  } else if (agentState.authMtimeMs !== mtime) {
    // auth.json was replaced since the failure (re-login + upload): the old verdict no longer applies.
    agentState.lastAuthState = "unknown";
    agentState.authMtimeMs = mtime;
    return "valid";
  }
  return "expired";
}

export function createAgentServer(deps: ServerDeps = {}): Server {
  const analyze = deps.runAnalysis ?? runAnalysis;
  const createRuntime = deps.createModelRuntime ?? ((options) => ModelRuntime.create(options));
  const say = deps.logger ?? ((line: string) => console.log(line));
  let active = 0;

  async function handleAnalyze(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const auth = authorized(req.headers.authorization);
    if (auth !== true) {
      req.resume();
      return send(res, auth === "no_token" ? 503 : 401, { error: { code: auth === "no_token" ? "token_unavailable" : "unauthorized" } });
    }
    const text = await readBody(req);
    if (text === null) return send(res, 400, errorResponse("bad_request", `请求体超过 ${MAX_BODY_BYTES / 1024} KB`));
    let raw: unknown;
    try {
      raw = JSON.parse(text);
    } catch {
      return send(res, 400, errorResponse("bad_request", "请求体不是合法 JSON"));
    }
    const parsed = parseAnalyzeRequest(raw);
    if (typeof parsed === "string") return send(res, 400, errorResponse("bad_request", parsed));
    if (active >= MAX_CONCURRENT) return send(res, 429, { error: { code: "busy" } });

    active++;
    try {
      let result: AnalyzeResponse;
      try {
        if (!hasCredential()) {
          // Checked before ModelRuntime.create, which would write an empty "{}" auth.json.
          agentState.lastAuthState = "missing";
          result = errorResponse("auth_missing", "ChatGPT 凭据缺失：auth.json 中没有 openai-codex 登录凭据");
        } else {
          // A fresh runtime per request: the credential file may have been re-uploaded.
          const modelRuntime = await createRuntime({ authPath: `${CONFIG.dataDir}/auth.json` });
          try {
            result = await analyze(parsed, { modelRuntime, fetchKeys: () => fetchAgentConfig() });
          } finally {
            // Persist an OAuth token refreshed during the session before dropping the runtime.
            await modelRuntime.settleCredentialWrites?.();
          }
        }
      } catch (err) {
        result = errorResponse("model_error", `AI 分析服务内部错误：${err instanceof Error ? err.message : String(err)}`);
      }
      // The auth verdict of this request belongs to the auth.json version it ran with (incl. refresh writes).
      agentState.authMtimeMs = authMtimeMs();
      say(
        `[server] analyze ${parsed.purpose} ${parsed.symbol} → ${result.status}` +
          `${result.error ? ` ${result.error.code}` : ""} tools=${result.usage.tool_calls} ${result.usage.duration_ms}ms` +
          ` model=${agentState.lastModel ?? "-"}`,
      );
      send(res, 200, result);
    } finally {
      active--;
    }
  }

  return createServer((req, res) => {
    const path = (req.url ?? "/").split("?")[0];
    if (req.method === "POST" && path === "/analyze") {
      handleAnalyze(req, res).catch(() => {
        if (!res.headersSent) send(res, 500, { error: { code: "internal" } });
      });
      return;
    }
    req.resume();
    if (req.method === "GET" && path === "/health") {
      return send(res, 200, {
        status: "ok",
        auth: authState(),
        revision: process.env.APP_REVISION ?? "",
        model: agentState.lastModel,
      });
    }
    send(res, 404, { error: { code: "not_found" } });
  });
}

function isEntry(): boolean {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return import.meta.url === pathToFileURL(realpathSync(entry)).href;
  } catch {
    return false;
  }
}

if (isEntry()) {
  const server = createAgentServer();
  server.listen(CONFIG.port, "0.0.0.0", () => {
    console.log(`[server] agent listening on :${CONFIG.port} revision=${process.env.APP_REVISION ?? "-"}`);
  });
  const stop = () => server.close(() => process.exit(0));
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

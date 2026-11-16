import { readFileSync } from "node:fs";

export const CONFIG = {
  port: 8080,
  mainUrl: process.env.MAIN_URL ?? "http://binance-bstocks-analysis-bot:8000",
  dataDir: process.env.AGENT_DATA_DIR ?? "/agent-data",
  sessionTimeoutMs: 300_000,
  maxToolCalls: 25,
  provider: "openai-codex",
};

export function readToken(): string {
  return readFileSync(`${CONFIG.dataDir}/internal_token`, "utf8").trim();
}

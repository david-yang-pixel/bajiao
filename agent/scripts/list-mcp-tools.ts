// Discovery helper: list every tool an MCP server exposes (no allow-list filtering).
// Usage: tsx scripts/list-mcp-tools.ts <openbb|alpaca> [--schemas]
// The server executables (openbb-mcp / alpaca-mcp-server) must be on PATH;
// alpaca reads ALPACA_API_KEY / ALPACA_SECRET_KEY, openbb's FRED key comes from FRED_API_KEY.
import { ToolLog } from "../src/toolLog.js";
import { buildServerSpecs, openMcpTools } from "../src/mcpBridge.js";

const [which, flag] = process.argv.slice(2);
const spec = buildServerSpecs({
  alpaca_api_key: process.env.ALPACA_API_KEY ?? "",
  alpaca_secret_key: process.env.ALPACA_SECRET_KEY ?? "",
  fred_api_key: process.env.FRED_API_KEY ?? "",
}).find((s) => s.name === which);
if (!spec) {
  console.error("usage: tsx scripts/list-mcp-tools.ts <openbb|alpaca> [--schemas]  (alpaca needs ALPACA_API_KEY/ALPACA_SECRET_KEY)");
  process.exit(2);
}
const r = await openMcpTools([{ ...spec, allow: () => true }], new ToolLog());
for (const w of r.warnings) console.error(w);
const prefix = `${spec.name}_`;
for (const t of r.tools) {
  const name = t.name.slice(prefix.length);
  if (flag === "--schemas") console.log(JSON.stringify({ name, description: t.description, inputSchema: t.parameters }));
  else console.log(name);
}
console.error(`${r.tools.length} tools`);
await r.close();

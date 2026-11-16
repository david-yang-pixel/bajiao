// Fake MCP server for tests: stdio transport, one read tool and one trading tool.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const server = new McpServer({ name: "fake", version: "0.0.1" });

server.registerTool(
  "get_news",
  { description: "Get news for a symbol", inputSchema: { symbol: z.string(), limit: z.number().optional() } },
  async ({ symbol }) => {
    if (symbol === "FAIL") return { content: [{ type: "text", text: "upstream error for FAIL" }], isError: true };
    return {
      content: [
        { type: "text", text: `news for ${symbol} https://example.com/news/${symbol}` },
        { type: "text", text: `pid=${process.pid}` },
      ],
    };
  },
);

server.registerTool(
  "place_order",
  { description: "Place an order", inputSchema: { symbol: z.string(), qty: z.number() } },
  async () => ({ content: [{ type: "text", text: "order placed" }] }),
);

await server.connect(new StdioServerTransport());

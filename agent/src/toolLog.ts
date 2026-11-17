import { defineTool } from "@earendil-works/pi-coding-agent";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { TSchema } from "typebox";
import { CONFIG } from "./config.js";

export interface ToolLogEntry {
  call_id: string;
  tool: string;
  args: unknown;
  summary: string;
  urls: string[];
  is_error: boolean;
}

const URL_RE = /https?:\/\/[^\s"'<>)\]]+/g;

export class ToolLimitError extends Error {
  constructor() {
    super(`工具调用次数已达上限(${CONFIG.maxToolCalls})，请直接调用 submit_pick 提交结论`);
    this.name = "ToolLimitError";
  }
}

export class ToolLog {
  private items: ToolLogEntry[] = [];

  record(tool: string, args: unknown): string {
    const call_id = `c${this.items.length + 1}`;
    this.items.push({ call_id, tool, args, summary: "", urls: [], is_error: false });
    return call_id;
  }

  complete(callId: string, resultText: string, isError: boolean): void {
    const e = this.items.find((i) => i.call_id === callId);
    if (!e) return;
    e.summary = resultText.slice(0, 1500);
    e.urls = [...new Set(resultText.match(URL_RE) ?? [])];
    e.is_error = isError;
  }

  entries(): ToolLogEntry[] {
    return this.items;
  }

  count(): number {
    return this.items.length;
  }
}

export interface WrapDef<P extends TSchema> {
  name: string;
  label: string;
  description: string;
  parameters: P;
  run: (params: any, signal?: AbortSignal) => Promise<string>;
}

export function wrapTool<P extends TSchema>(log: ToolLog, def: WrapDef<P>): ToolDefinition {
  return defineTool({
    name: def.name,
    label: def.label,
    description: def.description,
    parameters: def.parameters,
    async execute(_toolCallId, params, signal) {
      if (log.count() >= CONFIG.maxToolCalls) throw new ToolLimitError();
      const callId = log.record(def.name, params);
      try {
        const text = await def.run(params, signal);
        log.complete(callId, text, false);
        return { content: [{ type: "text", text: `[call_id=${callId}]\n${text}` }], details: {} };
      } catch (err) {
        log.complete(callId, err instanceof Error ? err.message : String(err), true);
        throw err;
      }
    },
  }) as ToolDefinition;
}

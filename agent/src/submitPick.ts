import { defineTool } from "@earendil-works/pi-coding-agent";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export interface SubmitPick {
  symbol: string;
  decision: string;
  current_price: number;
  target_price?: number;
  stop_price?: number;
  view?: string;
  rationale: string;
  invalidation: string;
  risks: string[];
  factor_highlights: string[];
  citations: { call_id: string; url?: string }[];
}

export type Purpose = "pick" | "position_review";

function schema(purpose: Purpose) {
  const values = purpose === "pick" ? ["selected", "rejected"] : ["hold", "reduce", "exit"];
  return Type.Object({
    symbol: Type.String(),
    decision: Type.Union(values.map((v) => Type.Literal(v))),
    current_price: Type.Number(),
    target_price: Type.Optional(Type.Number()),
    stop_price: Type.Optional(Type.Number()),
    view: Type.Optional(Type.String()),
    rationale: Type.String({ minLength: 20 }),
    invalidation: Type.String({ minLength: 8 }),
    risks: Type.Array(Type.String(), { minItems: 1 }),
    factor_highlights: Type.Array(Type.String()),
    citations: Type.Array(
      Type.Object({ call_id: Type.String(), url: Type.Optional(Type.String()) }),
      { minItems: 1 },
    ),
  });
}

export function createSubmitPickTool(purpose: Purpose, sink: { value: SubmitPick | null }): ToolDefinition {
  return defineTool({
    name: "submit_pick",
    label: "提交结论",
    description:
      "提交最终结论（只能调用一次）。必须引用前面工具返回的 call_id（可附 url）；价格均为正股 USD。",
    parameters: schema(purpose),
    async execute(_id, params) {
      let text = "已提交过，忽略";
      if (sink.value === null) {
        const pick = params as SubmitPick;
        // 持仓复核：Python 校验要求 view ∈ {hold,reduce,exit}，且需要 decision
        sink.value = purpose === "position_review" ? { ...pick, view: pick.decision } : pick;
        text = "已提交";
      }
      return { content: [{ type: "text", text }], details: {} };
    },
  }) as ToolDefinition;
}

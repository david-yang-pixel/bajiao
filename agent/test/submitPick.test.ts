import { describe, it, expect } from "vitest";
import { Value } from "typebox/value";
import { createSubmitPickTool, type SubmitPick } from "../src/submitPick.js";

const good = {
  symbol: "NVDA", decision: "selected", current_price: 100, target_price: 110, stop_price: 95,
  rationale: "动能强劲且基本面支撑，短线有望继续上行突破", invalidation: "收盘价跌破95美元止损位",
  risks: ["大盘回撤"], factor_highlights: ["rsi 60"], citations: [{ call_id: "c1", url: "https://a.com" }],
};
const run = (t: any, p: any) => t.execute("id", p, undefined, undefined, {} as any);

describe("submit_pick", () => {
  it("schema accepts good and rejects missing/empty citations", () => {
    const t = createSubmitPickTool("pick", { value: null });
    expect(Value.Check(t.parameters, good)).toBe(true);
    const { citations, ...rest } = good;
    expect(Value.Check(t.parameters, rest)).toBe(false);
    expect(Value.Check(t.parameters, { ...good, citations: [] })).toBe(false);
    expect(Value.Check(t.parameters, { ...good, risks: [] })).toBe(false);
    expect(Value.Check(t.parameters, { ...good, rationale: "短" })).toBe(false);
  });
  it("decision enum depends on purpose", () => {
    const p = createSubmitPickTool("pick", { value: null });
    const r = createSubmitPickTool("position_review", { value: null });
    expect(Value.Check(p.parameters, { ...good, decision: "hold" })).toBe(false);
    expect(Value.Check(r.parameters, good)).toBe(false);
    expect(Value.Check(r.parameters, { ...good, decision: "reduce" })).toBe(true);
  });
  it("accepts only first submission", async () => {
    const sink: { value: SubmitPick | null } = { value: null };
    const t = createSubmitPickTool("pick", sink);
    expect((await run(t, good)).content[0].text).toBe("已提交");
    expect((await run(t, { ...good, symbol: "TSLA" })).content[0].text).toBe("已提交过，忽略");
    expect(sink.value?.symbol).toBe("NVDA");
  });
  it("position_review sets view = decision", async () => {
    const sink: { value: SubmitPick | null } = { value: null };
    const t = createSubmitPickTool("position_review", sink);
    await run(t, { ...good, decision: "exit" });
    expect(sink.value?.view).toBe("exit");
  });
});

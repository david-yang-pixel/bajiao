import { describe, it, expect } from "vitest";
import { ToolLog, wrapTool, ToolLimitError } from "../src/toolLog.js";
import { Type } from "typebox";

const def = (run: (p: any) => Promise<string>) => ({
  name: "t", label: "T", description: "d", parameters: Type.Object({}), run,
});

describe("ToolLog", () => {
  it("assigns sequential call ids", () => {
    const l = new ToolLog();
    expect(l.record("a", {})).toBe("c1");
    expect(l.record("b", {})).toBe("c2");
    expect(l.count()).toBe(2);
  });
  it("extracts and dedupes urls, truncates summary", () => {
    const l = new ToolLog();
    const id = l.record("news", { q: 1 });
    const text = "x".repeat(1600) + ' see https://a.com/x?y=1 and "https://b.com/z" https://a.com/x?y=1';
    l.complete(id, text, false);
    const e = l.entries()[0];
    expect(e.summary.length).toBe(1500);
    expect(e.urls).toEqual(["https://a.com/x?y=1", "https://b.com/z"]);
    expect(e).toMatchObject({ call_id: "c1", tool: "news", args: { q: 1 }, is_error: false });
  });
});

describe("wrapTool", () => {
  it("prefixes call_id and logs result", async () => {
    const l = new ToolLog();
    const t = wrapTool(l, def(async () => "hello https://u.com/a"));
    const r = await t.execute("x", {}, undefined, undefined, {} as any);
    expect(r.content[0]).toEqual({ type: "text", text: "[call_id=c1]\nhello https://u.com/a" });
    expect(l.entries()[0].urls).toEqual(["https://u.com/a"]);
  });
  it("logs errors and rethrows", async () => {
    const l = new ToolLog();
    const t = wrapTool(l, def(async () => { throw new Error("boom"); }));
    await expect(t.execute("x", {}, undefined, undefined, {} as any)).rejects.toThrow("boom");
    expect(l.entries()[0]).toMatchObject({ is_error: true, summary: "boom" });
  });
  it("throws ToolLimitError after 25 calls", async () => {
    const l = new ToolLog();
    const t = wrapTool(l, def(async () => "ok"));
    for (let i = 0; i < 25; i++) await t.execute("x", {}, undefined, undefined, {} as any);
    await expect(t.execute("x", {}, undefined, undefined, {} as any)).rejects.toBeInstanceOf(ToolLimitError);
    expect(l.count()).toBe(25);
  });
});

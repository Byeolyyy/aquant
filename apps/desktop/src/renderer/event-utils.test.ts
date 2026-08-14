import { describe, expect, it } from "vitest";

import { eventSummary, isMessageEvent } from "./event-utils";

describe("eventSummary", () => {
  it("uses agent summary when present", () => {
    expect(
      eventSummary({
        event_id: "1",
        seq: 1,
        run_id: "run",
        kind: "agent.message",
        timestamp: "2026-07-21T12:00:00+08:00",
        agent_id: "quant_signal",
        payload: { summary: "量化检查完成" },
      })
    ).toBe("量化检查完成");
  });

  it("renders the coordinator final synthesis", () => {
    const summary = eventSummary({
      event_id: "2",
      seq: 2,
      run_id: "run",
      kind: "agent.message",
      timestamp: "2026-07-21T12:00:00+08:00",
      agent_id: "coordinator",
      payload: {
        executive_summary: "总体结论",
        signal_interpretation: ["信号一", "信号二"],
        news_summary: ["600000.SS｜浦发银行：消息面平稳"],
        risk_notes: "注意风险",
      },
    });
    expect(summary).toContain("总体结论");
    expect(summary).toContain("量化依据");
    expect(summary).toContain("• 信号一");
    expect(summary).toContain("消息面");
  });

  it("keeps Agent plans but filters Harness lifecycle events", () => {
    const base = {
      event_id: "3",
      seq: 3,
      run_id: "run",
      timestamp: "2026-07-21T12:00:00+08:00",
      payload: {},
    };
    expect(isMessageEvent({ ...base, kind: "task.plan", agent_id: "coordinator" })).toBe(true);
    expect(isMessageEvent({ ...base, kind: "run.status", agent_id: "" })).toBe(false);
  });
});

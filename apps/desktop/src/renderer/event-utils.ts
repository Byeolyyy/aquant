import type { HarnessEvent } from "../shared/protocol";

export const AGENT_NAMES: Record<string, string> = {
  coordinator: "统筹 Agent",
  quant_signal: "量化信号 Agent",
  company_industry: "公司与行业 Agent",
  global_market: "外围市场 Agent",
  risk: "风险 Agent",
  market_event: "市场事件 Agent（历史）",
  evidence_risk: "证据与风险 Agent（历史）",
};

export function eventSummary(event: HarnessEvent): string {
  const payload = event.payload;
  if (typeof payload.content === "string") return payload.content;
  if (typeof payload.summary === "string") return payload.summary;
  if (typeof payload.executive_summary === "string") {
    const sections = [payload.executive_summary];
    appendSection(sections, "信号解读", payload.signal_interpretation);
    appendSection(sections, "风险提示", payload.risk_notes);
    appendSection(sections, "证据缺口", payload.evidence_gaps);
    if (typeof payload.disclaimer === "string") sections.push(payload.disclaimer);
    return sections.join("\n\n");
  }
  if (event.kind === "task.plan") {
    const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
    return `我已发布 ${tasks.length} 项并行分析，并安排后续风险复核与最终综合。下面是完整流程。`;
  }
  return event.kind;
}

export function isMessageEvent(event: HarnessEvent): boolean {
  return Boolean(event.agent_id) && (event.kind === "agent.message" || event.kind === "task.plan");
}

function appendSection(sections: string[], title: string, value: unknown): void {
  const text = displayValue(value);
  if (text) sections.push(`${title}\n${text}`);
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) return value.map(displayValue).filter(Boolean).map((item) => `• ${item}`).join("\n");
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}：${displayValue(item)}`)
      .join("\n");
  }
  return value == null ? "" : String(value);
}

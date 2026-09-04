/**
 * 运行快照转 Markdown。
 *
 * 纯函数，不依赖 Electron 或 Node：桌面版走主进程的保存对话框写文件，
 * 网页版在浏览器里生成 Blob 下载，两端共用这一份实现。
 */

export function snapshotToMarkdown(snapshot: Record<string, unknown>): string {
  const final = (snapshot.final || {}) as Record<string, unknown>;
  const metrics = (snapshot.metrics || {}) as Record<string, unknown>;
  const events = Array.isArray(snapshot.events) ? snapshot.events as Array<Record<string, unknown>> : [];
  const names: Record<string, string> = {
    coordinator: "统筹 Agent",
    quant_signal: "量化信号 Agent",
    company_industry: "公司与行业 Agent",
    global_market: "外围市场 Agent",
    risk: "风险 Agent",
    market_event: "市场事件 Agent（历史）",
    evidence_risk: "证据与风险 Agent（历史）",
  };
  const lines = [
    `# ${String(final.title || "aquant 研究报告")}`,
    "",
    `- Run ID: \`${String(snapshot.run_id || "")}\``,
    `- 状态: ${String(snapshot.status || "")}`,
    `- 创建时间: ${String(snapshot.created_at || "")}`,
    `- 耗时: ${String(metrics.duration_seconds || 0)} 秒`,
    `- 证据: ${String(metrics.evidence_count || 0)} 条`,
    `- 风险项: ${String(metrics.risk_count || 0)} 条`,
    "",
    "## 最终综合",
    "",
    valueToText(final.executive_summary),
    "",
    "### 量化依据",
    "",
    valueToText(final.signal_interpretation),
    "",
    "### 消息面",
    "",
    valueToText(final.news_summary),
    "",
    "### 风险",
    "",
    valueToText(final.risk_notes),
    "",
    "### 证据缺口",
    "",
    valueToText(final.evidence_gaps),
  ];
  const evidence = new Map<string, Record<string, unknown>>();
  for (const event of events) {
    if (event.kind !== "agent.message") continue;
    const agentId = String(event.agent_id || "");
    const payload = (event.payload || {}) as Record<string, unknown>;
    if (Array.isArray(payload.evidence)) {
      for (const item of payload.evidence as Array<Record<string, unknown>>) {
        const evidenceId = String(item.evidence_id || "");
        if (evidenceId) evidence.set(evidenceId, item);
      }
    }
    const content = valueToText(payload.summary || payload.content);
    if (!content) continue;
    lines.push("", `## ${names[agentId] || agentId}`, "", content);
  }
  if (evidence.size) {
    lines.push("", "## 证据登记", "");
    for (const item of evidence.values()) {
      const title = String(item.title || "未命名资料");
      const url = String(item.url || "");
      const sourceType = String(item.source_type || "unknown");
      lines.push(`- ${url.startsWith("https://") ? `[${title}](${url})` : title} · ${sourceType} · ${String(item.published_at || "日期未知")}`);
    }
  }
  lines.push("", "---", "仅供研究解读与风险提示，不构成买卖或仓位建议。", "");
  return lines.join("\n");
}

export function valueToText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => `- ${valueToText(item)}`).join("\n");
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `- ${key}: ${valueToText(item)}`)
      .join("\n");
  }
  return value == null ? "" : String(value);
}

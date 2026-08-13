import React, { useEffect, useMemo, useState } from "react";

import { AGENT_NAMES } from "./event-utils";

export interface RunMetrics {
  duration_seconds: number;
  event_count: number;
  agent_count: number;
  evidence_count: number;
  source_count: number;
  risk_count: number;
  model_calls: number;
  fallback_count: number;
  error_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  agent_durations_ms?: Record<string, number>;
}

export interface RunSummary {
  run_id: string;
  report_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  generated_at: string;
  parse_status: string;
  symbols: string[];
  selected_count: number;
  near_count: number;
  title: string;
  summary: string;
  metrics: RunMetrics;
}

export interface AgentConfig {
  agent_id: string;
  display_name: string;
  lane: string;
  description: string;
  tool_allowlist: string[];
  enabled: boolean;
  required: boolean;
  custom_instructions: string;
  config_version: number;
  updated_at: string;
}

export interface PromptVersion {
  version_id: string;
  prompt_id: string;
  version_number: number;
  content: string;
  status: "draft" | "published" | "archived";
  change_note: string;
  created_at: string;
  published_at: string;
}

export interface PromptTemplate {
  prompt_id: string;
  agent_id: string;
  name: string;
  description: string;
  layer: "platform" | "system";
  locked: boolean;
  versions: PromptVersion[];
}

export interface WorkflowDefinition {
  workflow_id: string;
  version: number;
  mode: string;
  description: string;
  nodes: Array<{ node_id: string; name: string; kind: string }>;
}

export function MetricStrip({ metrics }: { metrics: Partial<RunMetrics> }) {
  const values = [
    ["耗时", `${metrics.duration_seconds || 0}s`],
    ["参与 Agent", String(metrics.agent_count || 0)],
    ["证据", String(metrics.evidence_count || 0)],
    ["风险项", String(metrics.risk_count || 0)],
    ["模型调用", String(metrics.model_calls || 0)],
  ];
  return (
    <div className="metric-strip">
      {values.map(([label, value]) => <div key={label}><span>{label}</span><b>{value}</b></div>)}
    </div>
  );
}

export function RunHistoryPanel({
  runs,
  loading,
  onOpen,
  onRetry,
  onExport,
}: {
  runs: RunSummary[];
  loading: boolean;
  onOpen: (run: RunSummary) => void;
  onRetry: (run: RunSummary) => void;
  onExport: (run: RunSummary) => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return runs;
    return runs.filter((run) =>
      `${run.title} ${run.status} ${run.symbols.join(" ")} ${run.run_id}`.toLowerCase().includes(keyword)
    );
  }, [query, runs]);
  const completed = runs.filter((run) => run.status === "completed").length;
  const totalEvidence = runs.reduce((sum, run) => sum + (run.metrics.evidence_count || 0), 0);
  const avgSeconds = runs.length
    ? Math.round(runs.reduce((sum, run) => sum + (run.metrics.duration_seconds || 0), 0) / runs.length)
    : 0;

  return (
    <section className="workspace-page">
      <div className="page-heading">
        <div><span className="eyebrow">RUN REGISTRY</span><h1>运行记录</h1><p>每次研究都保留任务、消息、证据、风险和模型调用审计。</p></div>
        <input className="search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索股票代码或 Run ID" />
      </div>
      <div className="overview-grid">
        <OverviewCard label="累计运行" value={runs.length} detail="SQLite 本地持久化" />
        <OverviewCard label="完成率" value={runs.length ? `${Math.round(completed / runs.length * 100)}%` : "—"} detail={`${completed} 次完成`} />
        <OverviewCard label="累计证据" value={totalEvidence} detail="跨正式与公开来源" />
        <OverviewCard label="平均耗时" value={`${avgSeconds}s`} detail="端到端运行" />
      </div>
      <div className="run-table">
        <div className="run-table-head"><span>运行</span><span>标的</span><span>观测</span><span>状态</span><span /></div>
        {loading && <div className="panel-empty">正在读取运行记录…</div>}
        {!loading && filtered.length === 0 && <div className="panel-empty">没有匹配的运行记录</div>}
        {filtered.map((run) => (
          <article className="run-row" key={run.run_id}>
            <button className="run-primary" onClick={() => onOpen(run)}>
              <b>{run.title}</b>
              <small>{formatDate(run.created_at)} · {run.run_id.slice(0, 8)}</small>
            </button>
            <div className="run-symbols">{run.symbols.slice(0, 4).map((symbol) => <code key={symbol}>{symbol}</code>)}{run.symbols.length > 4 && <span>+{run.symbols.length - 4}</span>}</div>
            <div className="run-observe"><b>{run.selected_count}</b><span>正式</span><b>{run.near_count}</b><span>near</span></div>
            <div><StatusBadge status={run.status} /><small className="run-duration">{run.metrics.duration_seconds || 0}s · {run.metrics.evidence_count || 0} 证据</small></div>
            <div className="row-actions">
              <button className="icon-button" title="导出 Markdown" onClick={() => onExport(run)}>⇩</button>
              <button className="icon-button" title="重新运行" onClick={() => onRetry(run)}>↻</button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

export function AgentGovernancePanel({
  agents,
  savingId,
  onSave,
}: {
  agents: AgentConfig[];
  savingId: string;
  onSave: (agent: AgentConfig, enabled: boolean, customInstructions: string) => void;
}) {
  return (
    <section className="workspace-page">
      <div className="page-heading">
        <div><span className="eyebrow">AGENT GOVERNANCE</span><h1>Agent 管理</h1><p>管理成员启停与项目级附加要求。核心治理角色保持锁定。</p></div>
        <div className="governance-badge"><i /> 配置版本化</div>
      </div>
      <div className="agent-config-grid">
        {agents.map((agent) => (
          <AgentConfigCard key={`${agent.agent_id}-${agent.config_version}`} agent={agent} saving={savingId === agent.agent_id} onSave={onSave} />
        ))}
      </div>
    </section>
  );
}

export function PromptWorkbenchPanel({
  prompts,
  workflows,
  busy,
  onCreateDraft,
  onPublish,
  onRollback,
}: {
  prompts: PromptTemplate[];
  workflows: WorkflowDefinition[];
  busy: boolean;
  onCreateDraft: (promptId: string, content: string, note: string) => void;
  onPublish: (versionId: string) => void;
  onRollback: (promptId: string, versionId: string) => void;
}) {
  const [selectedId, setSelectedId] = useState("");
  useEffect(() => {
    if (!selectedId && prompts.length) setSelectedId(prompts[0].prompt_id);
    if (selectedId && !prompts.some((prompt) => prompt.prompt_id === selectedId)) {
      setSelectedId(prompts[0]?.prompt_id || "");
    }
  }, [prompts, selectedId]);
  const selected = prompts.find((prompt) => prompt.prompt_id === selectedId) || prompts[0];
  return (
    <section className="workspace-page prompt-page">
      <div className="page-heading">
        <div><span className="eyebrow">PROMPT CONTROL PLANE</span><h1>Prompt 工作台</h1><p>查看有效系统指令，通过草稿、发布和回滚控制每次运行使用的版本。</p></div>
        <div className="governance-badge"><i /> 平台策略不可覆盖</div>
      </div>
      <div className="prompt-layout">
        <aside className="prompt-catalog">
          <h3>Prompt 模板</h3>
          {prompts.map((prompt) => {
            const published = prompt.versions.find((version) => version.status === "published");
            return (
              <button key={prompt.prompt_id} className={selected?.prompt_id === prompt.prompt_id ? "active" : ""} onClick={() => setSelectedId(prompt.prompt_id)}>
                <span className={`prompt-layer layer-${prompt.layer}`}>{prompt.layer === "platform" ? "策略" : "系统"}</span>
                <b>{prompt.name}</b>
                <small>{prompt.locked ? "只读" : `已发布 v${published?.version_number || 1}`}</small>
              </button>
            );
          })}
        </aside>
        <div className="prompt-editor-shell">
          {selected ? (
            <PromptEditor
              key={`${selected.prompt_id}-${selected.versions.length}-${selected.versions[0]?.status}`}
              prompt={selected}
              busy={busy}
              onCreateDraft={onCreateDraft}
              onPublish={onPublish}
              onRollback={onRollback}
            />
          ) : <div className="panel-empty">正在读取 Prompt…</div>}
        </div>
      </div>
      <div className="workflow-demo-section">
        <div className="subsection-heading"><div><span className="eyebrow">SUBGRAPH DEMO</span><h2>独立子工作流</h2></div><p>量化与外围市场 Agent 使用各自独立、可观测的内部工作流。</p></div>
        <div className="workflow-demo-grid">
          {workflows.map((workflow) => (
            <article className="workflow-demo-card" key={workflow.workflow_id}>
              <header><div><b>{workflow.workflow_id}</b><p>{workflow.description}</p></div><span>v{workflow.version}</span></header>
              <div className="workflow-node-track">
                {workflow.nodes.map((node, index) => (
                  <React.Fragment key={node.node_id}>
                    <div><span>{String(index + 1).padStart(2, "0")}</span><b>{node.name}</b><small>{node.kind}</small></div>
                    {index < workflow.nodes.length - 1 && <i>→</i>}
                  </React.Fragment>
                ))}
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function PromptEditor({
  prompt,
  busy,
  onCreateDraft,
  onPublish,
  onRollback,
}: {
  prompt: PromptTemplate;
  busy: boolean;
  onCreateDraft: (promptId: string, content: string, note: string) => void;
  onPublish: (versionId: string) => void;
  onRollback: (promptId: string, versionId: string) => void;
}) {
  const published = prompt.versions.find((version) => version.status === "published") || prompt.versions[0];
  const [selectedVersionId, setSelectedVersionId] = useState(published?.version_id || "");
  const selectedVersion = prompt.versions.find((version) => version.version_id === selectedVersionId) || published;
  const [content, setContent] = useState(selectedVersion?.content || "");
  const [note, setNote] = useState("");
  function selectVersion(version: PromptVersion) {
    setSelectedVersionId(version.version_id);
    setContent(version.content);
    setNote("");
  }
  const edited = content.trim() !== (selectedVersion?.content || "").trim();
  return (
    <div className="prompt-editor">
      <header>
        <div><span className={`prompt-layer layer-${prompt.layer}`}>{prompt.layer}</span><h2>{prompt.name}</h2><p>{prompt.description}</p></div>
        <div className="effective-version"><span>当前生效</span><b>v{published?.version_number || 1}</b></div>
      </header>
      <div className="prompt-editor-grid">
        <div className="prompt-content-editor">
          <div className="prompt-edit-meta"><span>{selectedVersion ? `v${selectedVersion.version_number}` : ""}</span><span className={`version-status version-${selectedVersion?.status}`}>{versionLabel(selectedVersion?.status)}</span><small>{content.length.toLocaleString()} 字符</small></div>
          <textarea value={content} readOnly={prompt.locked} onChange={(event) => setContent(event.target.value)} spellCheck={false} />
          {!prompt.locked && <div className="prompt-publish-bar">
            <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="本次修改说明" maxLength={500} />
            <button className="ghost" disabled={busy || !edited || content.trim().length < 40} onClick={() => onCreateDraft(prompt.prompt_id, content, note)}>保存为新草稿</button>
            {selectedVersion?.status === "draft" && <button className="primary" disabled={busy} onClick={() => onPublish(selectedVersion.version_id)}>发布 v{selectedVersion.version_number}</button>}
            {selectedVersion?.status === "archived" && <button className="primary" disabled={busy} onClick={() => onRollback(prompt.prompt_id, selectedVersion.version_id)}>回滚并发布</button>}
          </div>}
          {prompt.locked && <div className="locked-policy-note">平台策略由 Harness 强制拼接，项目说明和统筹任务都不能覆盖。</div>}
        </div>
        <aside className="version-history"><h3>版本记录</h3>{prompt.versions.map((version) => (
          <button key={version.version_id} className={selectedVersion?.version_id === version.version_id ? "active" : ""} onClick={() => selectVersion(version)}>
            <span>v{version.version_number}</span><b>{versionLabel(version.status)}</b><small>{version.change_note || "没有版本说明"}</small><time>{formatDate(version.published_at || version.created_at)}</time>
          </button>
        ))}</aside>
      </div>
    </div>
  );
}

function versionLabel(status?: string): string {
  return ({ published: "已发布", draft: "草稿", archived: "已归档" } as Record<string, string>)[status || ""] || status || "";
}

function AgentConfigCard({
  agent,
  saving,
  onSave,
}: {
  agent: AgentConfig;
  saving: boolean;
  onSave: (agent: AgentConfig, enabled: boolean, customInstructions: string) => void;
}) {
  const [enabled, setEnabled] = useState(agent.enabled);
  const [instructions, setInstructions] = useState(agent.custom_instructions);
  const dirty = enabled !== agent.enabled || instructions !== agent.custom_instructions;
  return (
    <article className={`agent-config-card ${enabled ? "enabled" : "disabled"}`}>
      <header>
        <span className={`avatar avatar-${agent.agent_id}`}>{(AGENT_NAMES[agent.agent_id] || agent.display_name).slice(0, 1)}</span>
        <div><h2>{agent.display_name}</h2><p>{agent.description}</p></div>
        <label className={`toggle ${agent.required ? "locked" : ""}`} title={agent.required ? "核心治理角色不可停用" : "启用或停用"}>
          <input type="checkbox" checked={enabled} disabled={agent.required} onChange={(event) => setEnabled(event.target.checked)} />
          <span />
        </label>
      </header>
      <div className="agent-meta"><span>{agent.lane}</span><span>v{agent.config_version}</span>{agent.required && <span>核心角色</span>}</div>
      <div className="tool-chips">{agent.tool_allowlist.map((tool) => <code key={tool}>{tool}</code>)}</div>
      <label className="instruction-field">
        <span>项目级附加要求</span>
        <textarea value={instructions} onChange={(event) => setInstructions(event.target.value)} placeholder="例如：优先检查最近 30 天的减持、问询和诉讼公告。" maxLength={4000} />
      </label>
      <footer><small>{instructions.length}/4000 · {agent.updated_at ? `更新于 ${formatDate(agent.updated_at)}` : "默认配置"}</small><button className="primary" disabled={!dirty || saving} onClick={() => onSave(agent, enabled, instructions)}>{saving ? "保存中…" : "保存配置"}</button></footer>
    </article>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const labels: Record<string, string> = {
    completed: "已完成", failed: "失败", cancelled: "已取消", planning: "规划中",
    starting: "启动中", specialists_running: "分析中", risk_review: "风险复核", synthesizing: "汇总中", paused: "已暂停",
  };
  return <span className={`status-badge status-badge-${status}`}>{labels[status] || status}</span>;
}

function OverviewCard({ label, value, detail }: { label: string; value: string | number; detail: string }) {
  return <article className="overview-card"><span>{label}</span><b>{value}</b><small>{detail}</small></article>;
}

function formatDate(value: string): string {
  if (!value) return "未知时间";
  const normalized = value.includes("T") ? value : value.replace(" ", "T") + "Z";
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

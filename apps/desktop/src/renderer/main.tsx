import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";

import type { HarnessEvent } from "../shared/protocol";
import { AGENT_NAMES, eventSummary, isMessageEvent } from "./event-utils";
import { SettingsModal, type SettingsData } from "./settings-modal";
import {
  AgentGovernancePanel,
  MetricStrip,
  PromptWorkbenchPanel,
  RunHistoryPanel,
  StatusBadge,
  type AgentConfig,
  type PromptTemplate,
  type RunMetrics,
  type RunSummary,
  type WorkflowDefinition,
} from "./workspace-panels";
import "./styles.css";

interface StockRow {
  symbol: string;
  reason: string;
  source_pool: string;
  realtime_formula_wanyuan: string | null;
  flow_threshold_wanyuan: string | null;
  vol_ratio: string | null;
  turnover_now_pct: string | null;
  l4_buy_sell: boolean | null;
  missing_fields: string[];
}

interface ParsedReport {
  report_id: string;
  generated_at: string;
  run_slot: string;
  parse_status: "valid" | "partial" | "invalid";
  selected_rows: StockRow[];
  near_rows: StockRow[];
  diagnostics: string[];
  parse_errors: string[];
}

interface AgentTask {
  task_id: string;
  agent_id: string;
  title: string;
  instructions?: string;
  symbols?: string[];
  config_version?: number;
  prompt_version?: string;
  workflow_id?: string;
  workflow_version?: number;
}

interface EvidenceItem {
  evidence_id: string;
  source_type: string;
  title: string;
  excerpt?: string;
  url?: string;
  published_at?: string;
  retrieved_at?: string;
  symbols?: string[];
}

interface MarketHistoryPoint {
  date: string;
  close: number;
}

interface MarketIndexItem {
  ticker: string;
  name: string;
  region: string;
  currency: string;
  trade_date: string;
  timezone: string;
  close: number;
  previous_close: number;
  change: number;
  change_percent: number;
  history: MarketHistoryPoint[];
  source_url?: string;
}

interface GlobalMarketData {
  market_indices: MarketIndexItem[];
  status: "live_delayed" | "demo_fallback" | string;
  provider: string;
  retrieved_at: string;
  notice: string;
}

type WorkspaceView = "research" | "runs" | "agents" | "prompts";

interface RunSnapshot {
  run_id: string;
  report_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  final: Record<string, unknown> | null;
  events: HarnessEvent[];
  metrics: RunMetrics;
}

const SAMPLE_REPORT = `生成时间: 2026-07-31 14:30:00
运行轮次: 1430
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell super_large_anomaly
600000.SS all_conditions_met 4300 4000 1.20 2.50 True False
near_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell super_large_anomaly
000001.SZ near_miss 3500 4000 1.05 3.10 False False`;

const ROSTER = ["coordinator", "quant_signal", "company_industry", "global_market", "risk"] as const;

function App() {
  const [service, setService] = useState<"connecting" | "ready" | "error">("connecting");
  const [serviceDetail, setServiceDetail] = useState("正在启动本地 Harness…");
  const [rawText, setRawText] = useState(SAMPLE_REPORT);
  const [report, setReport] = useState<ParsedReport | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<HarnessEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [steering, setSteering] = useState("");
  const [error, setError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [activeView, setActiveView] = useState<WorkspaceView>("research");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [agents, setAgents] = useState<AgentConfig[]>([]);
  const [savingAgentId, setSavingAgentId] = useState("");
  const [prompts, setPrompts] = useState<PromptTemplate[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>([]);
  const [promptBusy, setPromptBusy] = useState(false);
  const [snapshotMetrics, setSnapshotMetrics] = useState<RunMetrics | null>(null);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!window.quantAgent) {
      setService("error");
      setServiceDetail("桌面桥接未加载，请重新构建或重新安装应用");
      return;
    }
    const unsubscribeEvent = window.quantAgent.onEvent((event) => {
      setEvents((current) => {
        if (current.some((item) => item.event_id === event.event_id)) return current;
        return [...current, event].sort((a, b) => a.seq - b.seq);
      });
      if (event.kind === "run.completed" || event.kind === "run.error") void loadRuns();
    });
    const unsubscribeCrash = window.quantAgent.onCrash((message) => {
      setService("error");
      setServiceDetail(message);
    });
    window.quantAgent
      .request("ping")
      .then((result) => {
        setService("ready");
        setServiceDetail(`${String(result.service)} · ${String(result.mode)}`);
      })
      .catch((reason: Error) => {
        setService("error");
        setServiceDetail(reason.message);
      });
    window.quantAgent
      .request("get_settings")
      .then((result) => setSettings(result.settings as unknown as SettingsData))
      .catch(() => undefined);
    void loadRuns();
    void loadAgents();
    void loadPrompts();
    void loadWorkflows();
    return () => {
      unsubscribeEvent();
      unsubscribeCrash();
    };
  }, []);

  const runEvents = useMemo(
    () => (runId ? events.filter((event) => event.run_id === runId) : []),
    [events, runId]
  );
  const messages = runEvents.filter(isMessageEvent);
  const taskPlan = runEvents.find((event) => event.kind === "task.plan");
  const plannedWorkflow =
    ((taskPlan?.payload.workflow_steps as AgentTask[] | undefined) ||
      (taskPlan?.payload.tasks as AgentTask[] | undefined) || []);
  const selectedAgents = new Set(
    plannedWorkflow
      .map((task) => task.agent_id || "")
      .filter(Boolean)
  );
  selectedAgents.add("coordinator");
  if (runEvents.some((event) => event.agent_id === "risk")) selectedAgents.add("risk");
  const lastStatus = [...runEvents]
    .reverse()
    .find((event) => event.kind === "run.status" || event.kind === "run.completed" || event.kind === "run.error");
  const status = String(lastStatus?.payload.status || (runId ? "starting" : "draft"));
  const stocks = report ? [...report.selected_rows, ...report.near_rows] : [];
  const liveMetrics = useMemo(() => calculateLiveMetrics(runEvents), [runEvents]);
  const currentMetrics = runEvents.length ? liveMetrics : snapshotMetrics;

  async function loadRuns() {
    setRunsLoading(true);
    try {
      const result = await window.quantAgent.request("list_runs", { limit: 100 });
      setRuns((result.runs || []) as unknown as RunSummary[]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRunsLoading(false);
    }
  }

  async function loadAgents() {
    try {
      const result = await window.quantAgent.request("get_agents");
      setAgents((result.agents || []) as unknown as AgentConfig[]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function loadPrompts() {
    try {
      const result = await window.quantAgent.request("get_prompt_workspace");
      setPrompts((result.prompts || []) as unknown as PromptTemplate[]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function loadWorkflows() {
    try {
      const result = await window.quantAgent.request("get_workflows");
      setWorkflows((result.workflows || []) as unknown as WorkflowDefinition[]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function createPromptDraft(promptId: string, content: string, changeNote: string) {
    setPromptBusy(true);
    setError("");
    try {
      const result = await window.quantAgent.request("create_prompt_draft", {
        prompt_id: promptId,
        content,
        change_note: changeNote,
      });
      setPrompts((result.prompts || []) as unknown as PromptTemplate[]);
      setNotice("新草稿已保存；发布后才会被新运行使用。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPromptBusy(false);
    }
  }

  async function publishPromptVersion(versionId: string) {
    setPromptBusy(true);
    setError("");
    try {
      const result = await window.quantAgent.request("publish_prompt_version", { version_id: versionId });
      setPrompts((result.prompts || []) as unknown as PromptTemplate[]);
      setNotice("Prompt 已发布，新运行将绑定该版本。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPromptBusy(false);
    }
  }

  async function rollbackPromptVersion(promptId: string, versionId: string) {
    setPromptBusy(true);
    setError("");
    try {
      const result = await window.quantAgent.request("rollback_prompt_version", {
        prompt_id: promptId,
        source_version_id: versionId,
      });
      setPrompts((result.prompts || []) as unknown as PromptTemplate[]);
      setNotice("已从历史内容创建并发布一个新版本，旧审计记录保持不变。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPromptBusy(false);
    }
  }

  function startNewResearch() {
    setActiveView("research");
    setReport(null);
    setRunId(null);
    setEvents([]);
    setSnapshotMetrics(null);
    setError("");
  }

  async function openRun(run: RunSummary) {
    setBusy(true);
    setError("");
    try {
      const result = await window.quantAgent.request("get_run_snapshot", { run_id: run.run_id });
      const snapshot = result.snapshot as unknown as RunSnapshot;
      setRunId(snapshot.run_id);
      setReport(null);
      setEvents(snapshot.events);
      setSnapshotMetrics(snapshot.metrics);
      setActiveView("research");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function retryRun(run: RunSummary) {
    setBusy(true);
    setError("");
    try {
      const result = await window.quantAgent.request("retry_run", { run_id: run.run_id });
      setRunId(String(result.run_id));
      setReport(null);
      setEvents([]);
      setSnapshotMetrics(null);
      setActiveView("research");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function exportRun(run: RunSummary) {
    try {
      const result = await window.quantAgent.exportRun(run.run_id);
      if (result.exported) setNotice(`已导出到 ${result.path || "所选位置"}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function saveAgentConfig(agent: AgentConfig, enabled: boolean, customInstructions: string) {
    setSavingAgentId(agent.agent_id);
    setError("");
    try {
      const result = await window.quantAgent.request("save_agent_config", {
        agent_id: agent.agent_id,
        enabled,
        custom_instructions: customInstructions,
      });
      setAgents((result.agents || []) as unknown as AgentConfig[]);
      setNotice(`${agent.display_name} 配置已保存，新运行将使用该版本。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSavingAgentId("");
    }
  }

  async function parseReport() {
    setBusy(true);
    setError("");
    setRunId(null);
    setEvents([]);
    try {
      const result = await window.quantAgent.request("parse_report", { raw_text: rawText });
      setReport(result.report as unknown as ParsedReport);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function startRun() {
    if (!report) return;
    setBusy(true);
    setError("");
    try {
      const result = await window.quantAgent.request("start_run", { report_id: report.report_id });
      setRunId(String(result.run_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function control(method: string) {
    if (!runId) return;
    try {
      await window.quantAgent.request(method, { run_id: runId });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function sendSteering() {
    if (!runId || !steering.trim()) return;
    const message = steering.trim();
    setSteering("");
    try {
      await window.quantAgent.request("steer_run", { run_id: runId, message });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark">Q</div>
        <div>
          <strong>QuantAgent</strong>
        </div>
        <div className={`service-chip ${service}`} title={serviceDetail}>
          <i />
          {service === "ready" ? "服务正常" : service === "connecting" ? "正在启动" : "服务异常"}
        </div>
        <div className="topbar-actions">
          <button className="primary compact" onClick={startNewResearch}>＋ 新建研究</button>
          <button className="ghost" onClick={() => setSettingsOpen(true)}>连接与密钥</button>
        </div>
      </header>

      <aside className="sidebar">
        <div className="section-label">工作台</div>
        <nav className="workspace-nav">
          <button className={activeView === "research" ? "active" : ""} onClick={() => setActiveView("research")}><span>⌁</span><b>实时研究</b></button>
          <button className={activeView === "runs" ? "active" : ""} onClick={() => { setActiveView("runs"); void loadRuns(); }}><span>◫</span><b>运行记录</b><small>{runs.length}</small></button>
          <button className={activeView === "agents" ? "active" : ""} onClick={() => { setActiveView("agents"); void loadAgents(); }}><span>◇</span><b>Agent 管理</b></button>
          <button className={activeView === "prompts" ? "active" : ""} onClick={() => { setActiveView("prompts"); void loadPrompts(); void loadWorkflows(); }}><span>⌘</span><b>Prompt 工作台</b></button>
        </nav>
        <div className="section-label agents-label">Agent 团队</div>
        {ROSTER.map((agentId) => (
          <div className="agent-row" key={agentId}>
            <span className={`avatar avatar-${agentId}`}>{AGENT_NAMES[agentId].slice(0, 1)}</span>
            <span><b>{AGENT_NAMES[agentId]}</b></span>
            <i className={selectedAgents.has(agentId) || agents.find((item) => item.agent_id === agentId)?.enabled ? "online" : "idle"} />
          </div>
        ))}
        <div className="sidebar-footer">
          <span>密钥本地加密保存</span>
        </div>
      </aside>

      {activeView === "research" && <main className="conversation">
        <div className="channel-header">
          <div><h1># PTrade 实时研究</h1></div>
          {runId ? <StatusBadge status={status} /> : <span className="draft-badge">等待报告</span>}
        </div>

        <div className="conversation-scroll">
          {!report && (
            <section className="composer-card hero-card">
              <div className="eyebrow">新研究事件</div>
              <h2>粘贴一份 PTrade 原始报告</h2>
              <p>先预览解析结果，确认后再启动多 Agent 研究。</p>
              <textarea value={rawText} onChange={(event) => setRawText(event.target.value)} spellCheck={false} />
              <div className="card-actions">
                <span>{rawText.length.toLocaleString()} 字符</span>
                <button className="primary" disabled={busy || !rawText.trim()} onClick={parseReport}>
                  {busy ? "正在解析…" : "解析并预览"}
                </button>
              </div>
            </section>
          )}

          {report && !runId && (
            <section className="preview-card">
              <div className="preview-heading">
                <div><div className="eyebrow">结构化预览</div><h2>{report.generated_at || "未识别报告时间"}</h2></div>
                <span className={`quality quality-${report.parse_status}`}>{report.parse_status}</span>
              </div>
              {(report.parse_errors.length > 0 || report.diagnostics.length > 0) && (
                <div className="diagnostics">
                  {[...report.parse_errors, ...report.diagnostics].map((item) => <div key={item}>• {item}</div>)}
                </div>
              )}
              <div className="table-wrap">
                <table>
                  <thead><tr><th>标的</th><th>来源池</th><th>资金公式</th><th>门槛</th><th>量比</th><th>换手%</th><th>买盘</th><th>缺失</th></tr></thead>
                  <tbody>
                    {stocks.map((stock) => (
                      <tr key={`${stock.source_pool}-${stock.symbol}`}>
                        <td><b>{stock.symbol}</b><small>{stock.reason}</small></td>
                        <td><span className={`pool pool-${stock.source_pool}`}>{stock.source_pool}</span></td>
                        <td>{stock.realtime_formula_wanyuan ?? "缺失"}</td>
                        <td>{stock.flow_threshold_wanyuan ?? "缺失"}</td>
                        <td>{stock.vol_ratio ?? "缺失"}</td>
                        <td>{stock.turnover_now_pct ?? "缺失"}</td>
                        <td>{stock.l4_buy_sell === null ? "缺失" : stock.l4_buy_sell ? "通过" : "未通过"}</td>
                        <td>{stock.missing_fields.length ? stock.missing_fields.join(", ") : "—"}</td>
                      </tr>
                    ))}
                    {stocks.length === 0 && <tr><td colSpan={8} className="empty-cell">报告中两个池均为空</td></tr>}
                  </tbody>
                </table>
              </div>
              <div className="card-actions">
                <button className="ghost" onClick={() => setReport(null)}>返回修改原文</button>
                <button className="primary" disabled={busy || report.parse_status === "invalid"} onClick={startRun}>
                  {report.parse_status === "partial" ? "确认风险并启动研究" : "确认并启动多 Agent 研究"}
                </button>
              </div>
            </section>
          )}

          {runId && (
            <section className="message-list">
              {messages.map((event) => (
                <article className={`message ${event.agent_id === "coordinator" ? "coordinator-message" : ""}`} key={event.event_id}>
                  <span className={`avatar avatar-${event.agent_id || "system"}`}>
                    {event.agent_id ? AGENT_NAMES[event.agent_id]?.slice(0, 1) || "A" : "•"}
                  </span>
                  <div className="message-content">
                    <div className="message-meta">
                      <b>{event.agent_id ? AGENT_NAMES[event.agent_id] || event.agent_id : "Harness"}</b>
                      <span>{new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
                    </div>
                    <AgentMessageBody event={event} />
                    {Array.isArray(event.payload.evidence) && event.payload.evidence.length > 0 && (
                      <EvidenceList evidence={event.payload.evidence as EvidenceItem[]} />
                    )}
                    {Array.isArray(event.payload.risks) && event.payload.risks.length > 0 && (
                      <div className="risk-box">{event.payload.risks.map((risk) => <div key={String(risk)}>⚠ {String(risk)}</div>)}</div>
                    )}
                    {Array.isArray(event.payload.unknowns) && event.payload.unknowns.length > 0 && (
                      <div className="unknown-box">{event.payload.unknowns.map((item) => <div key={String(item)}>待核验 · {String(item)}</div>)}</div>
                    )}
                  </div>
                </article>
              ))}
              {messages.length === 0 && <div className="working-pulse"><i /><span>统筹 Agent 正在读取报告并组建团队…</span></div>}
            </section>
          )}
          {(error || lastStatus?.kind === "run.error") && <div className="error-banner">{error || String(lastStatus?.payload.error || "运行失败")}</div>}
        </div>

        {runId && !["completed", "cancelled"].includes(status) && (
          <div className="steering-bar">
            <input value={steering} onChange={(event) => setSteering(event.target.value)} placeholder="补充指令将在下一个节点边界交给统筹 Agent…" onKeyDown={(event) => event.key === "Enter" && void sendSteering()} />
            <button className="ghost" onClick={() => control("pause_run")}>暂停</button>
            <button className="danger" onClick={() => control("cancel_run")}>取消</button>
            <button className="primary" disabled={!steering.trim()} onClick={sendSteering}>发送</button>
          </div>
        )}
      </main>}

      {activeView === "runs" && <main className="conversation workspace-main">
        <RunHistoryPanel runs={runs} loading={runsLoading} onOpen={openRun} onRetry={retryRun} onExport={exportRun} />
      </main>}

      {activeView === "agents" && <main className="conversation workspace-main">
        <AgentGovernancePanel agents={agents} savingId={savingAgentId} onSave={saveAgentConfig} />
      </main>}

      {activeView === "prompts" && <main className="conversation workspace-main">
        <PromptWorkbenchPanel
          prompts={prompts}
          workflows={workflows}
          busy={promptBusy}
          onCreateDraft={createPromptDraft}
          onPublish={publishPromptVersion}
          onRollback={rollbackPromptVersion}
        />
      </main>}

      <aside className="inspector">
        {activeView === "research" && <>
          <div className="inspector-title"><span>本轮运行</span>{runId && <code>{runId.slice(0, 8)}</code>}</div>
          {currentMetrics && <section><h3>实时观测</h3><MetricStrip metrics={currentMetrics} /></section>}
          <section>
            <h3>执行流程</h3>
            {!taskPlan && <p className="muted">启动研究后，这里会显示统筹 Agent 的完整任务链。</p>}
            {plannedWorkflow.map((task) => (
              <div className="task-card" key={task.task_id}>
                <i />
                <div>
                  <b>{task.title}</b>
                  <small>{AGENT_NAMES[task.agent_id] || task.agent_id} · 配置 v{task.config_version || 1}</small>
                  {task.workflow_id && <small>{task.workflow_id} v{task.workflow_version || 1}</small>}
                  {task.prompt_version && <small>{task.prompt_version}</small>}
                </div>
              </div>
            ))}
          </section>
          <section>
            <h3>资料连接</h3>
            <div className="connection-list">
              <ConnectionRow label="统筹模型" configured={Boolean(settings?.model.ready)} />
              <ConnectionRow label="Tushare" configured={Boolean(settings?.tushare.token_configured)} />
              <ConnectionRow label="行业联网检索" configured={Boolean(settings?.tavily.api_key_configured)} />
              <ConnectionRow label="外围指数延迟行情" configured />
              <ConnectionRow label="公开公告与新闻" configured />
            </div>
            <button className="configure-button" onClick={() => setSettingsOpen(true)}>配置连接</button>
          </section>
        </>}
        {activeView === "runs" && <>
          <div className="inspector-title"><span>审计能力</span></div>
          <section className="info-stack"><div><b>事件溯源</b><p>每个 Run 按序保存任务、消息、模型调用与失败回退。</p></div><div><b>可重复执行</b><p>从相同报告创建新 Run，不覆盖旧记录。</p></div><div><b>研究导出</b><p>一键导出 Markdown，保留 Agent 结论与免责声明。</p></div></section>
        </>}
        {activeView === "agents" && <>
          <div className="inspector-title"><span>治理规则</span></div>
          <section className="info-stack"><div><b>最小权限</b><p>每个 Agent 只使用 allowlist 中的只读工具。</p></div><div><b>核心角色锁定</b><p>统筹、量化和风险 Agent 构成不可绕过的治理链。</p></div><div><b>配置版本</b><p>每次保存都会增加版本，新运行读取最新配置。</p></div></section>
        </>}
        {activeView === "prompts" && <>
          <div className="inspector-title"><span>生效规则</span></div>
          <section className="info-stack">
            <div><b>平台策略层</b><p>安全、真实性、工具权限和提示注入防护固定在最上层，项目 Prompt 无法覆盖。</p></div>
            <div><b>发布即绑定</b><p>只有已发布版本会进入新 Run；草稿可以反复修改，不影响正在运行的任务。</p></div>
            <div><b>完整审计</b><p>发布与回滚都生成不可变版本记录，任务保存实际使用的 Prompt 版本。</p></div>
            <div><b>外部内容不可信</b><p>检索到的网页、公告与研报只作为资料，不能改变 Agent 身份和工具边界。</p></div>
          </section>
        </>}
      </aside>
      {(notice || error) && <div className={`toast ${error ? "error" : ""}`} onClick={() => { setNotice(""); setError(""); }}>{error || notice}</div>}
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={(value) => {
          setSettings(value);
          setServiceDetail(value.model.ready ? `quant-agent-harness · ${value.model.model}` : "quant-agent-harness · deterministic-demo");
        }}
      />
    </div>
  );
}

function ConnectionRow({ label, configured }: { label: string; configured: boolean }) {
  return <div className="connection-row"><span><i className={configured ? "connected" : ""} />{label}</span><b>{configured ? "已配置" : "未配置"}</b></div>;
}

function AgentMessageBody({ event }: { event: HarnessEvent }) {
  if (event.kind === "task.plan") {
    const tasks =
      (event.payload.workflow_steps as AgentTask[] | undefined) ||
      (event.payload.tasks as AgentTask[] | undefined) || [];
    return (
      <div className="plan-message">
        <p>{eventSummary(event)}</p>
        <div className="plan-grid">
          {tasks.map((task, index) => (
            <div className="plan-assignment" key={task.task_id}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <b>{task.title}</b>
                <small>{AGENT_NAMES[task.agent_id] || task.agent_id} · 配置 v{task.config_version || 1} · {task.prompt_version || "agent-prompts-v2"}</small>
                {task.instructions && <p>{task.instructions}</p>}
                {Boolean(task.symbols?.length) && <div className="symbol-list">{task.symbols!.map((symbol) => <code key={symbol}>{symbol}</code>)}</div>}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const selectedAgents = Array.isArray(event.payload.selected_agents)
    ? event.payload.selected_agents.map(String)
    : [];
  return (
    <>
      <p>{eventSummary(event)}</p>
      {event.agent_id === "global_market" && event.payload.structured_data && (
        <GlobalMarketCard data={event.payload.structured_data as unknown as GlobalMarketData} />
      )}
      {selectedAgents.length > 0 && (
        <div className="team-assembly">
          <span>本轮参与</span>
          <div>{selectedAgents.map((agentId) => <b key={agentId}>{AGENT_NAMES[agentId] || agentId}</b>)}</div>
          {typeof event.payload.selection_rationale === "string" && <small>{event.payload.selection_rationale}</small>}
        </div>
      )}
    </>
  );
}

function GlobalMarketCard({ data }: { data: GlobalMarketData }) {
  const indices = Array.isArray(data.market_indices) ? data.market_indices : [];
  if (!indices.length) return null;
  return (
    <section className={`global-market-card ${data.status === "demo_fallback" ? "demo" : ""}`}>
      <header>
        <div><span>GLOBAL SESSION</span><b>外围指数温度</b></div>
        <span className="market-data-status">{data.status === "demo_fallback" ? "演示数据" : "延迟行情"}</span>
      </header>
      <div className="market-index-grid">
        {indices.map((item) => <MarketIndexCard item={item} key={item.ticker} />)}
      </div>
      <footer>
        <span>{data.notice}</span>
        <small>{data.provider} · 获取于 {formatMarketTime(data.retrieved_at)}</small>
      </footer>
    </section>
  );
}

function MarketIndexCard({ item }: { item: MarketIndexItem }) {
  const positive = item.change_percent >= 0;
  return (
    <article className={`market-index-card ${positive ? "positive" : "negative"}`}>
      <div className="market-index-heading">
        <div><b>{item.name}</b><code>{item.ticker}</code></div>
        <strong>{positive ? "+" : ""}{item.change_percent.toFixed(2)}%</strong>
      </div>
      <Sparkline points={item.history || []} positive={positive} />
      <div className="market-index-footer">
        <span>{Number(item.close).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}</span>
        <small>{item.trade_date} · {shortTimezone(item.timezone)}</small>
      </div>
    </article>
  );
}

function Sparkline({ points, positive }: { points: MarketHistoryPoint[]; positive: boolean }) {
  if (points.length < 2) return <div className="sparkline-empty">历史点不足</div>;
  const width = 120;
  const height = 38;
  const values = points.map((point) => Number(point.close));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(max - min, Math.abs(max) * 0.002, 1);
  const coords = values.map((value, index) => {
    const x = index / Math.max(1, values.length - 1) * width;
    const y = height - 4 - ((value - min) / span) * (height - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const area = `0,${height} ${coords} ${width},${height}`;
  return (
    <svg className="market-sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-label="最近五个交易日走势">
      <polygon points={area} className={positive ? "spark-fill-positive" : "spark-fill-negative"} />
      <polyline points={coords} className={positive ? "spark-positive" : "spark-negative"} />
    </svg>
  );
}

function shortTimezone(value: string): string {
  return value === "America/New_York" ? "纽约" : value === "Asia/Seoul" ? "首尔" : value || "当地";
}

function formatMarketTime(value: string): string {
  if (!value) return "未知时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function EvidenceList({ evidence }: { evidence: EvidenceItem[] }) {
  return (
    <details className="evidence-list">
      <summary>查看 {evidence.length} 条资料来源</summary>
      <div className="evidence-cards">
        {evidence.map((item) => (
          <article className="evidence-card" key={item.evidence_id}>
            <div>
              <span>{evidenceSourceLabel(item.source_type)}</span>
              {item.published_at && <time>{item.published_at}</time>}
            </div>
            {item.url ? <a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a> : <b>{item.title}</b>}
            {item.excerpt && <p>{item.excerpt}</p>}
          </article>
        ))}
      </div>
    </details>
  );
}

function evidenceSourceLabel(sourceType: string): string {
  return ({
    report: "PTrade 报告",
    tushare: "Tushare",
    tavily: "联网搜索",
    official_web: "官方公告",
    public_web: "新闻与研报",
    local_history: "本地资料",
    market_data: "指数行情",
  } as Record<string, string>)[sourceType] || sourceType;
}

function calculateLiveMetrics(events: HarnessEvent[]): RunMetrics {
  const agents = new Set<string>();
  const evidence = new Set<string>();
  const sources = new Set<string>();
  let risks = 0;
  let modelCalls = 0;
  let fallbackCount = 0;
  let errorCount = 0;
  let promptTokens = 0;
  let completionTokens = 0;
  const durations: Record<string, number> = {};
  for (const event of events) {
    if (event.kind === "agent.message" && event.agent_id) {
      agents.add(event.agent_id);
      const items = Array.isArray(event.payload.evidence) ? event.payload.evidence as Array<Record<string, unknown>> : [];
      for (const item of items) {
        if (item.evidence_id) evidence.add(String(item.evidence_id));
        if (item.source_type) sources.add(String(item.source_type));
      }
      if (Array.isArray(event.payload.risks)) risks += event.payload.risks.length;
    } else if (event.kind === "model.usage") {
      modelCalls += 1;
      promptTokens += Number(event.payload.prompt_tokens || 0);
      completionTokens += Number(event.payload.completion_tokens || 0);
    } else if (event.kind === "model.fallback") fallbackCount += 1;
    else if (event.kind === "run.error") errorCount += 1;
    else if (event.kind === "agent.lifecycle" && event.payload.status === "completed" && event.agent_id) {
      durations[event.agent_id] = Number(event.payload.duration_ms || 0);
    }
  }
  const first = events[0] ? new Date(events[0].timestamp).getTime() : Date.now();
  const last = events.at(-1) ? new Date(events.at(-1)!.timestamp).getTime() : first;
  return {
    duration_seconds: Math.max(0, Math.round((last - first) / 1000)),
    event_count: events.length,
    agent_count: agents.size,
    evidence_count: evidence.size,
    source_count: sources.size,
    risk_count: risks,
    model_calls: modelCalls,
    fallback_count: fallbackCount,
    error_count: errorCount,
    prompt_tokens: promptTokens,
    completion_tokens: completionTokens,
    agent_durations_ms: durations,
  };
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode><App /></React.StrictMode>
);

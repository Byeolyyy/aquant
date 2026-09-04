from __future__ import annotations

import threading
import json
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from .integrations import TavilyClient, TushareClient, normalize_ts_code
from .models import (
    DEFAULT_AGENT_PROFILES,
    AgentContribution,
    AgentTask,
    Claim,
    EvidenceItem,
    HarnessEvent,
    LaneSummary,
    ParsedReport,
    ReportStock,
    ResearchState,
    RunPolicy,
    TriggerRecord,
)
from .llm import OpenAICompatibleClient
from .parser import tiered_flow_threshold_wanyuan
from .repository import Repository
from .public_sources import PublicAStockClient
from .global_markets import GlobalMarketClient
from .agent_prompts import (
    AGENT_PROMPT_IDS,
    AGENT_PROMPTS,
    BRAIN_PLANNING_PROMPT,
    BRAIN_REVIEW_PROMPT,
    BRAIN_SYNTHESIS_PROMPT,
    RISK_PROMPT,
    PLATFORM_POLICY_PROMPT,
)
from .workflows import WORKFLOW_DEFINITIONS, workflow_definition
from .event_rules import (
    EVENT_SECTOR_MOVE_THRESHOLD_PCT,
    SECTOR_TRANSMISSION_MAP,
    EventRuleEngine,
    RuleContext,
)


EventSink = Callable[[HarnessEvent], None]

RISK_KEYWORDS = (
    "减持", "质押", "冻结", "诉讼", "仲裁", "立案", "调查", "问询", "处罚", "警示函",
    "监管措施", "退市", "亏损", "预亏", "下修", "解禁", "担保", "资金占用", "非标意见",
    "风险警示", "违约", "逾期", "停产", "事故", "召回", "减值", "业绩下降", "净利润下降",
    "破产", "重整", "失信", "辞职", "失联",
)

# 常驻泳道：由能力规则兜底；事件泳道可由代码扳机或大脑点名触发，每 run 每 Agent ≤1 次。
_RESIDENT_LANES = ("quantitative", "fundamental", "global_market", "review")
_EVENT_AGENT_IDS = {"capital_trace", "bearish_analysis", "global_sector_flow", "sector_transmission"}

_PROFILE_BY_ID = {profile.agent_id: profile for profile in DEFAULT_AGENT_PROFILES}
_PROFILE_LANE = {profile.agent_id: profile.lane for profile in DEFAULT_AGENT_PROFILES}
_RESIDENT_LANE_AGENTS = {
    profile.lane: profile.agent_id
    for profile in DEFAULT_AGENT_PROFILES
    if profile.lane in _RESIDENT_LANES
}

_CAPITAL_CLASSIFICATION_LABELS = {
    "single_day_pulse": "单日脉冲：当日资金异动缺乏历史延续，可能是短期情绪或事件驱动",
    "persistent_inflow": "持续流入：近 5 日中至少 3 日主力净流入为正，资金行为有延续性",
    "persistent_outflow": "持续流出：近 5 日中至少 3 日主力净流出为正，注意资金持续撤出",
    "insufficient_data": "资金历史数据不足，本轮无法判断持续性",
}


class CapabilityRegistry:
    """Maps work lanes to agents; the coordinator chooses lanes per report."""

    def select(self, report: ParsedReport, repository: Repository) -> list[str]:
        enabled = repository.enabled_agent_ids()
        selected = ["quant_signal"]
        if report.stocks and "company_industry" in enabled:
            selected.append("company_industry")
        if "global_market" in enabled:
            selected.append("global_market")
        return selected


class RunControl:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.steering: list[str] = []
        self._lock = threading.Lock()

    def wait_if_paused(self) -> None:
        while self.paused.is_set() and not self.cancelled.is_set():
            self.cancelled.wait(0.1)

    def add_steering(self, message: str) -> None:
        with self._lock:
            self.steering.append(message)

    def take_steering(self) -> list[str]:
        with self._lock:
            items = list(self.steering)
            self.steering.clear()
            return items


class Harness:
    def __init__(
        self,
        repository: Repository,
        event_sink: EventSink | None = None,
        llm_client: OpenAICompatibleClient | None = None,
        tushare_client: TushareClient | None = None,
        tavily_client: TavilyClient | None = None,
        public_a_stock_client: PublicAStockClient | None = None,
        global_market_client: GlobalMarketClient | None = None,
    ):
        self.repository = repository
        self.event_sink = event_sink or (lambda _event: None)
        self.llm_client = llm_client
        self.tushare_client = tushare_client
        self.tavily_client = tavily_client
        self.public_a_stock_client = public_a_stock_client
        self.global_market_client = global_market_client or GlobalMarketClient()
        self.registry = CapabilityRegistry()
        self._controls: dict[str, RunControl] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(
        self,
        report_id: str,
        policy: RunPolicy | None = None,
        *,
        owner_session: str | None = None,
    ) -> str:
        report = self.repository.get_report(report_id)
        if report is None:
            raise ValueError(f"找不到报告: {report_id}")
        if report.parse_status == "invalid":
            raise ValueError("解析状态为 invalid，不能启动 Agent 分析")
        run_id = str(uuid4())
        # owner_session 只在 Web 多用户模式下有值；桌面版传 None，
        # 查询时不过滤，行为与加这一列之前完全一致。
        self.repository.create_run(run_id, report_id, owner_session=owner_session)
        self._controls[run_id] = RunControl()
        self._seq[run_id] = 0
        thread = threading.Thread(
            target=self._run,
            args=(run_id, report, policy or RunPolicy()),
            name=f"quant-agent-run-{run_id[:8]}",
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def pause(self, run_id: str) -> None:
        self._control(run_id).paused.set()
        self._emit(run_id, "run.paused", payload={"status": "paused"})
        self.repository.update_run(run_id, "paused")

    def resume(self, run_id: str) -> None:
        self._control(run_id).paused.clear()
        self._emit(run_id, "run.resumed", payload={"status": "running"})
        self.repository.update_run(run_id, "running")

    def cancel(self, run_id: str) -> None:
        self._control(run_id).cancelled.set()

    def steer(self, run_id: str, message: str) -> None:
        if not message.strip():
            raise ValueError("插话内容不能为空")
        self._control(run_id).add_steering(message.strip())
        self._emit(run_id, "user.steering_queued", payload={"content": message.strip()})

    def active_run_count(self) -> int:
        """还在跑的运行数。Web 模式据此限制并发——服务器只有 2 核 2G。"""
        return sum(1 for thread in self._threads.values() if thread.is_alive())

    def wait(self, run_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(run_id)
        if not thread:
            return
        thread.join(timeout)
        # 静默超时会让调用方拿着被截断的事件列表继续断言，失败现场看起来
        # 像业务逻辑出错，实际只是没跑完。这里显式报错，指向真正的原因。
        if thread.is_alive():
            raise TimeoutError(f"运行 {run_id} 在 {timeout} 秒内未结束")

    def _control(self, run_id: str) -> RunControl:
        control = self._controls.get(run_id)
        if control is None:
            raise ValueError(f"找不到活动运行: {run_id}")
        return control

    def _run(self, run_id: str, report: ParsedReport, policy: RunPolicy) -> None:
        control = self._control(run_id)
        try:
            self._hydrate_report_security_names(report)
            self.repository.update_run(run_id, "planning")
            self._emit(run_id, "run.status", payload={"status": "planning"})
            brain_plan = self._brain_planning(run_id, report)
            state = ResearchState(
                round=0,
                core_questions=list(brain_plan.get("core_questions") or []),
                pending_needs=list(brain_plan.get("agent_calls") or []),
            )
            self._emit_research_state(run_id, state)
            symbols = [row.symbol for row in report.stocks[: policy.max_external_symbols]]
            resident_tasks, event_tasks, selected_agents, selection_rationale = (
                self._neck_dispatch_initial(run_id, report, state.pending_needs, policy)
            )
            state.needs_history = list(state.pending_needs)
            state.pending_needs = []
            self._emit_research_state(run_id, state)
            team_text = "、".join(_agent_display_name(agent_id) for agent_id in selected_agents)
            strategy_text = str(brain_plan.get("research_strategy") or "").strip()
            self._emit(
                run_id,
                "agent.message",
                agent_id="coordinator",
                payload={
                    "content": (
                        f"大脑的研究策略：{strategy_text}\n\n"
                        f"我作为秘书按泳道安排 {team_text} Agent 分头分析"
                        + (f"，并在常规分析后派发大脑点名的 {len(event_tasks)} 项专项调查" if event_tasks else "")
                        + "，之后由风险 Agent 逐票检索近期利空消息，最后由大脑综合。"
                    ),
                    "selected_agents": selected_agents,
                    "symbols": symbols,
                    "selection_rationale": selection_rationale,
                    "core_questions": state.core_questions,
                    "research_strategy": strategy_text,
                    "brain_agent_calls": [dict(call) for call in state.needs_history],
                    "engine": "openai-compatible" if self.llm_client else "deterministic-demo",
                },
            )
            agent_configs = {
                item["agent_id"]: item for item in self.repository.list_agent_configs()
            }
            review_task = AgentTask(
                run_id=run_id,
                agent_id="risk",
                title="逐票检索近期负面公告与新闻",
                instructions="按股票查询可追溯的潜在利空消息，过滤报告日之后的资料并通俗总结。",
                symbols=symbols,
                config_version=int((agent_configs.get("risk") or {}).get("config_version") or 1),
                prompt_version=self._prompt(AGENT_PROMPT_IDS["risk"], RISK_PROMPT)[1],
                workflow_id=str(workflow_definition("risk")["workflow_id"]),
                workflow_version=int(workflow_definition("risk")["version"]),
            )
            synthesis_task = AgentTask(
                run_id=run_id,
                agent_id="brain",
                title="吸收全部泳道与风险结果并形成最终综合",
                instructions="区分事实、解释、风险和证据缺口，输出研究解读与跨域定性，而非交易建议。",
                symbols=symbols,
                config_version=int((agent_configs.get("brain") or {}).get("config_version") or 1),
                prompt_version=self._prompt(AGENT_PROMPT_IDS["brain"], BRAIN_SYNTHESIS_PROMPT)[1],
            )
            self._emit(
                run_id,
                "task.plan",
                agent_id="coordinator",
                payload={
                    "tasks": [task.model_dump(mode="json") for task in resident_tasks],
                    "workflow_steps": [
                        *[task.model_dump(mode="json") for task in resident_tasks],
                        review_task.model_dump(mode="json"),
                        synthesis_task.model_dump(mode="json"),
                    ],
                },
            )
            self.repository.update_run(run_id, "specialists_running")
            self._emit(run_id, "run.status", payload={"status": "specialists_running"})

            contributions: list[AgentContribution] = []
            with ThreadPoolExecutor(max_workers=max(1, len(resident_tasks))) as executor:
                futures = {
                    executor.submit(self._execute_task, task, report, control): task
                    for task in resident_tasks
                }
                for future in as_completed(futures):
                    if control.cancelled.is_set():
                        break
                    contribution = future.result()
                    contributions.append(contribution)
                    self._emit(
                        run_id,
                        "agent.message",
                        agent_id=contribution.agent_id,
                        payload=contribution.model_dump(mode="json"),
                    )

            if control.cancelled.is_set():
                self.repository.update_run(run_id, "cancelled")
                self._emit(run_id, "run.completed", payload={"status": "cancelled"})
                return

            control.wait_if_paused()
            steering = control.take_steering()
            if steering:
                self._emit(
                    run_id,
                    "agent.message",
                    agent_id="coordinator",
                    payload={"content": "已在节点边界接收你的补充指令。", "steering": steering},
                )

            fired_agents: set[str] = set()
            fired_scopes: dict[str, set[str]] = {}
            # checkpoint 1：常驻泳道完成后求值事件规则（资金追查 / 外围板块资金及链式）
            contributions.extend(
                self._event_dispatch(
                    run_id,
                    "specialists_done",
                    report,
                    contributions,
                    None,
                    control,
                    policy,
                    state,
                    fired_agents,
                    fired_scopes,
                )
            )
            # 大脑规划阶段点名的专项调查（顺序执行，预算同事件规则；
            # 已由规则触发过的 Agent 只补查尚未覆盖的标的）
            if event_tasks:
                contributions = self._execute_brain_calls(
                    run_id,
                    report,
                    event_tasks,
                    contributions,
                    control,
                    policy,
                    state,
                    fired_agents,
                    fired_scopes,
                )

            self.repository.update_run(run_id, "risk_review")
            self._emit(run_id, "run.status", payload={"status": "risk_review"})
            self._emit(
                run_id,
                "agent.message",
                agent_id="coordinator",
                payload={
                    "content": "专业 Agent 已完成并行分析。现在由风险 Agent 逐票查询近期负面公告和新闻。",
                    "stage": "risk_review_handoff",
                    "received_from": [item.agent_id for item in contributions],
                    "assigned_to": "risk",
                },
            )
            review_started = time.perf_counter()
            self._emit(
                run_id,
                "agent.lifecycle",
                agent_id="risk",
                payload={"status": "started", "stage": "risk_review"},
            )
            self._emit_workflow_plan(review_task)
            review = self._review(run_id, report, contributions, review_task)
            contributions.append(review)
            self._emit(
                run_id,
                "agent.lifecycle",
                agent_id="risk",
                payload={
                    "status": "completed",
                    "stage": "risk_review",
                    "duration_ms": int((time.perf_counter() - review_started) * 1000),
                    "risk_count": len(review.risks),
                },
            )
            self._emit(
                run_id,
                "agent.message",
                agent_id="risk",
                payload=review.model_dump(mode="json"),
            )

            # checkpoint 2：风险审阅完成后求值事件规则（利空分析）
            contributions.extend(
                self._event_dispatch(
                    run_id,
                    "risk_done",
                    report,
                    contributions,
                    review,
                    control,
                    policy,
                    state,
                    fired_agents,
                    fired_scopes,
                )
            )

            # 大脑审阅循环（颈部执行）：压缩状态 → 大脑决策 → 任务书 → 顺序执行
            contributions, review, state, _rounds_used = self._neck_follow_up_loop(
                run_id,
                report,
                contributions,
                review,
                state,
                control,
                max_rounds=max(0, policy.max_brain_rounds),
                policy=policy,
                fired_agents=fired_agents,
                fired_scopes=fired_scopes,
            )

            self.repository.update_run(run_id, "synthesizing")
            self._emit(run_id, "run.status", payload={"status": "synthesizing"})
            if review.risks:
                self._emit(
                    run_id,
                    "agent.message",
                    agent_id="coordinator",
                    payload={
                        "content": "风险 Agent 发现了需要纳入结论的利空线索，大脑将结合来源和其他分析进行最终综合。",
                        "stage": "synthesis_handoff",
                        "reviewed_by": "risk",
                    },
                )
            final = self._synthesize(
                run_id, report, contributions, review, steering, state=state
            )
            self._emit(run_id, "agent.message", agent_id="coordinator", payload=final)
            self.repository.update_run(run_id, "completed", final)
            self._emit(run_id, "run.completed", payload={"status": "completed", "final": final})
        except Exception as exc:
            self.repository.update_run(run_id, "failed", {"error": str(exc)})
            self._emit(run_id, "run.error", payload={"error": f"{type(exc).__name__}: {exc}"})

    def _neck_follow_up_loop(
        self,
        run_id: str,
        report: ParsedReport,
        contributions: list[AgentContribution],
        review: AgentContribution,
        state: ResearchState,
        control: RunControl,
        *,
        max_rounds: int,
        policy: RunPolicy,
        fired_agents: set[str],
        fired_scopes: dict[str, set[str]],
    ) -> tuple[list[AgentContribution], AgentContribution, ResearchState, int]:
        """颈部循环：压缩研究状态 → 大脑审阅决策 → 任务书派发；决策归大脑，执行归颈部。"""

        dispatched: set[str] = set()
        rounds_used = 0
        decision: dict = {"decision": "finish", "review_summary": "", "agent_calls": []}
        while rounds_used < max_rounds:
            state = self._neck_compress(contributions, review, state, round=rounds_used)
            self._emit_research_state(run_id, state)
            decision = self._brain_review(
                run_id,
                report,
                state,
                remaining_rounds=max_rounds - rounds_used,
                previous_needs=dispatched,
            )
            agent_calls = list(decision.get("agent_calls") or [])
            if str(decision.get("decision")) != "continue" or not agent_calls:
                break
            tasks = self._neck_build_follow_up_tasks(
                run_id, report, agent_calls, policy, fired_agents, fired_scopes
            )
            if not tasks:
                break
            for task in tasks:
                state.needs_history.append(
                    {
                        "agent_id": task.agent_id,
                        "question": task.instructions.replace("大脑专项指令：", ""),
                        "symbols": list(task.symbols),
                        "reason": "大脑审阅补充",
                        "priority": 1,
                    }
                )
            summary = str(decision.get("review_summary") or "大脑认为需要补充信息。")
            assignment_text = "；".join(
                f"{_agent_display_name(task.agent_id)}：{task.instructions.replace('大脑专项指令：', '')}"
                for task in tasks
            )
            self._emit(
                run_id,
                "agent.message",
                agent_id="coordinator",
                payload={
                    "content": f"大脑审阅：{summary}\n追加安排：{assignment_text}",
                    "stage": "brain_review",
                    "decision": "continue",
                    "tasks": [task.model_dump(mode="json") for task in tasks],
                    "round": rounds_used + 1,
                },
            )
            self._emit(
                run_id,
                "task.replan",
                agent_id="coordinator",
                payload={
                    "phase": "brain_review",
                    "round": rounds_used + 1,
                    "tasks": [task.model_dump(mode="json") for task in tasks],
                },
            )
            for task in tasks:
                control.wait_if_paused()
                if control.cancelled.is_set():
                    return contributions, review, state, rounds_used
                if task.agent_id == "risk":
                    started = time.perf_counter()
                    self._emit(
                        run_id,
                        "agent.lifecycle",
                        agent_id="risk",
                        payload={"status": "started", "stage": "brain_review"},
                    )
                    self._emit_workflow_plan(task)
                    result = self._review(run_id, report, contributions, task)
                    review = result
                    self._emit(
                        run_id,
                        "agent.lifecycle",
                        agent_id="risk",
                        payload={
                            "status": "completed",
                            "stage": "brain_review",
                            "duration_ms": int((time.perf_counter() - started) * 1000),
                            "risk_count": len(result.risks),
                        },
                    )
                else:
                    result = self._execute_task(task, report, control)
                    contributions.append(result)
                if task.agent_id in _EVENT_AGENT_IDS:
                    if task.agent_id in ("global_sector_flow", "sector_transmission"):
                        fired_scopes[task.agent_id] = {"*"}
                    else:
                        fired_scopes[task.agent_id] = (
                            fired_scopes.get(task.agent_id) or set()
                        ) | set(task.symbols)
                self._emit(
                    run_id,
                    "agent.message",
                    agent_id=result.agent_id,
                    payload={
                        **result.model_dump(mode="json"),
                        "stage": "brain_review_result",
                        "requested_by": "brain",
                    },
                )
            rounds_used += 1
        state.terminated_reason = (
            "budget_exhausted"
            if str(decision.get("decision")) == "continue" and rounds_used >= max_rounds
            else "brain_finish"
        )
        return contributions, review, state, rounds_used

    def _brain_review(
        self,
        run_id: str,
        report: ParsedReport,
        state: ResearchState,
        *,
        remaining_rounds: int,
        previous_needs: set[str],
    ) -> dict:
        if self.llm_client is None:
            return {
                "decision": "finish",
                "review_summary": "当前未配置可用模型，大脑无法进行自主补充判断，已按现有结果继续。",
                "agent_calls": [],
            }
        system = self._prompt("brain.review", BRAIN_REVIEW_PROMPT)[0]
        valid_symbols = {row.symbol for row in report.stocks}
        available = self._available_callable_agents()
        payload = {
            "report": {
                "report_date": report.report_date,
                "parse_status": report.parse_status,
                "symbols": sorted(valid_symbols),
            },
            "agents": [
                {
                    "agent_id": agent_id,
                    "display_name": _agent_display_name(agent_id),
                    "responsibility": _agent_responsibility(agent_id),
                    "description": _PROFILE_BY_ID[agent_id].description,
                }
                for agent_id in available
            ],
            "remaining_rounds": remaining_rounds,
            "research_state": state.model_dump(mode="json"),
        }
        try:
            result = self.llm_client.complete_json(system, json.dumps(payload, ensure_ascii=False, default=str))
            raw_decision = str(result.data.get("decision") or "finish").lower()
            review_summary = str(result.data.get("review_summary") or "").strip()[:1200]
            agent_calls = []
            if raw_decision == "continue" and remaining_rounds > 0:
                for raw in list(result.data.get("agent_calls") or [])[:3]:
                    if not isinstance(raw, dict):
                        continue
                    agent_id = str(raw.get("agent_id") or "")
                    question = str(raw.get("question") or "").strip()[:1000]
                    symbols = [str(item) for item in raw.get("symbols") or [] if str(item) in valid_symbols][:3]
                    reason = str(raw.get("reason") or "").strip()[:500]
                    priority = int(raw.get("priority") or 2)
                    if agent_id not in available or len(question) < 8:
                        continue
                    signature = _follow_up_signature(agent_id, question, symbols)
                    if signature in previous_needs:
                        continue
                    previous_needs.add(signature)
                    agent_calls.append(
                        {
                            "agent_id": agent_id,
                            "question": question,
                            "symbols": symbols,
                            "reason": reason or "大脑审阅认为该问题会影响最终结论",
                            "priority": priority,
                        }
                    )
            if not agent_calls:
                raw_decision = "finish"
            self._emit(
                run_id,
                "model.usage",
                agent_id="brain",
                payload={
                    "stage": "review",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return {
                "decision": raw_decision,
                "review_summary": review_summary or "大脑已检查研究状态，未发现必须追加的有效信息。",
                "agent_calls": agent_calls,
            }
        except Exception as exc:
            self._emit(
                run_id,
                "model.fallback",
                agent_id="brain",
                payload={"stage": "review", "error": f"{type(exc).__name__}: {exc}"},
            )
            return {
                "decision": "finish",
                "review_summary": "大脑审阅模型本轮不可用，已保留现有结果并停止追加调用。",
                "agent_calls": [],
            }

    def _neck_build_follow_up_tasks(
        self,
        run_id: str,
        report: ParsedReport,
        calls: list[dict],
        policy: RunPolicy,
        fired_agents: set[str],
        fired_scopes: dict[str, set[str]],
    ) -> list[AgentTask]:
        # 去重已在 _brain_review 完成（签名加入 previous_needs 集合），
        # 这里做 agent 校验（事件 Agent 按覆盖度与预算）与任务书构造。
        enabled = self.repository.enabled_agent_ids()
        configs = {item["agent_id"]: item for item in self.repository.list_agent_configs()}
        valid_symbols = {row.symbol for row in report.stocks}
        tasks: list[AgentTask] = []
        for call in calls[:3]:
            agent_id = str(call.get("agent_id") or "")
            question = str(call.get("question") or "").strip()
            if agent_id not in enabled or not question:
                continue
            symbols = [str(item) for item in call.get("symbols") or [] if str(item) in valid_symbols][:3]
            if agent_id in _EVENT_AGENT_IDS:
                covered = fired_scopes.get(agent_id, set())
                if covered == {"*"}:
                    continue
                if agent_id in fired_agents:
                    uncovered = sorted(set(symbols) - covered)
                    if not uncovered:
                        continue
                    symbols = uncovered
                elif len(fired_agents) >= policy.max_event_agent_calls:
                    continue
                fired_agents.add(agent_id)
            definition = workflow_definition(agent_id)
            tasks.append(
                AgentTask(
                    run_id=run_id,
                    agent_id=agent_id,
                    title="大脑追问 · " + self._task_title(agent_id),
                    instructions="大脑专项指令：" + question,
                    symbols=symbols,
                    config_version=int((configs.get(agent_id) or {}).get("config_version") or 1),
                    prompt_version=self._prompt(
                        AGENT_PROMPT_IDS[agent_id],
                        RISK_PROMPT if agent_id == "risk" else AGENT_PROMPTS.get(agent_id, ""),
                    )[1],
                    workflow_id=str(definition["workflow_id"]),
                    workflow_version=int(definition["version"]),
                )
            )
        return tasks

    def _brain_planning(self, run_id: str, report: ParsedReport) -> dict:
        """大脑初始规划：研究策略 + 核心问题 + 自主点名的专项调查指令。"""
        fallback = self._default_brain_plan(report)
        if self.llm_client is None:
            return fallback
        system = self._prompt("brain.planning", BRAIN_PLANNING_PROMPT)[0]
        valid_symbols = {row.symbol for row in report.stocks}
        formal = [
            {"symbol": row.symbol, "name": row.name}
            for row in _formal_recommendation_rows(report)
        ]
        anomalies = [
            {"symbol": row.symbol, "super_large_anomaly": True}
            for row in report.stocks
            if row.super_large_anomaly is True
        ]
        available = self._available_callable_agents()
        compact = {
            "parse_status": report.parse_status,
            "generated_at": report.generated_at,
            "selected_count": len(report.selected_rows),
            "near_count": len(report.near_rows),
            "symbols": [row.symbol for row in report.stocks],
            "diagnostics": report.diagnostics,
            "deterministic_quant": {"formal": formal, "anomalies": anomalies},
            "agents": [
                {
                    "agent_id": agent_id,
                    "display_name": _agent_display_name(agent_id),
                    "responsibility": _agent_responsibility(agent_id),
                    "description": _PROFILE_BY_ID[agent_id].description,
                }
                for agent_id in available
            ],
        }
        try:
            result = self.llm_client.complete_json(system, json.dumps(compact, ensure_ascii=False, default=str))
            core_questions = [
                str(question).strip()
                for question in result.data.get("core_questions") or []
                if str(question).strip()
            ][:4]
            calls = _validated_agent_calls(
                result.data.get("agent_calls"), available, valid_symbols, limit=3
            )
            self._emit(
                run_id,
                "model.usage",
                agent_id="brain",
                payload={
                    "stage": "planning",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return {
                "research_strategy": str(result.data.get("research_strategy") or "").strip()[:600],
                "core_questions": core_questions or fallback["core_questions"],
                "agent_calls": calls or fallback["agent_calls"],
            }
        except Exception as exc:
            self._emit(
                run_id,
                "model.fallback",
                agent_id="brain",
                payload={"stage": "planning", "error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback

    def _default_brain_plan(self, report: ParsedReport) -> dict:
        """无模型或模型不可用时的确定性研究计划：只保留常驻泳道兜底。"""
        core_questions = [
            "量化信号是否可靠？",
            "公司与行业背景如何？",
            "外围市场环境如何？",
            "存在哪些潜在利空？",
        ]
        return {
            "research_strategy": "常规研究：由常驻泳道完成量化复核、公司背景、外围市场与风险检索，事件规则会在出现异常信号时自动追加专项调查。",
            "core_questions": core_questions,
            "agent_calls": [],
        }

    def _available_callable_agents(self) -> list[str]:
        """大脑可点名调查的 Agent：注册、启用、且不是大脑/秘书本人。"""
        return [
            profile.agent_id
            for profile in DEFAULT_AGENT_PROFILES
            if profile.agent_id not in {"brain", "coordinator"}
            and profile.agent_id in self.repository.enabled_agent_ids()
        ]

    def _neck_dispatch_initial(
        self,
        run_id: str,
        report: ParsedReport,
        calls: list[dict],
        policy: RunPolicy,
    ) -> tuple[list[AgentTask], list[AgentTask], list[str], str]:
        """颈部初始派单：常驻泳道由能力规则兜底；大脑点名的常驻 Agent 需求合并进任务书，专项 Agent 需求另列待顺序执行。"""
        selected_agents = self.registry.select(report, self.repository)
        agent_configs = {item["agent_id"]: item for item in self.repository.list_agent_configs()}
        workflow_configs = {
            agent_id: workflow_definition(agent_id) for agent_id in selected_agents
        }
        symbols = [row.symbol for row in report.stocks[: policy.max_external_symbols]]
        resident_tasks: list[AgentTask] = []
        for agent_id in selected_agents:
            lines = ["基于当前报告完成职责范围内的分析；不得补全缺失事实。"]
            for call in calls:
                if call.get("agent_id") == agent_id:
                    lines.append(
                        f"大脑专项指令：{call.get('question')}（原因：{call.get('reason')}）"
                    )
            custom = self.repository.agent_custom_instructions(agent_id)
            if custom:
                lines.append("本项目附加要求：" + custom)
            resident_tasks.append(
                AgentTask(
                    run_id=run_id,
                    agent_id=agent_id,
                    title=self._task_title(agent_id),
                    instructions="\n".join(lines),
                    symbols=symbols,
                    config_version=int((agent_configs.get(agent_id) or {}).get("config_version") or 1),
                    prompt_version=self._prompt(
                        AGENT_PROMPT_IDS[agent_id], AGENT_PROMPTS[agent_id]
                    )[1],
                    workflow_id=str(workflow_configs[agent_id]["workflow_id"]),
                    workflow_version=int(workflow_configs[agent_id]["version"]),
                )
            )
        event_tasks: list[AgentTask] = [
            self._brain_call_task(
                run_id, report, call, agent_configs, policy, title_prefix="大脑规划"
            )
            for call in calls
            if call.get("agent_id") not in selected_agents
        ]
        rationale = "常驻泳道由本地能力规则兜底；大脑的专项指令由秘书按序派发。"
        return resident_tasks, event_tasks, selected_agents, rationale

    def _brain_call_task(
        self,
        run_id: str,
        report: ParsedReport,
        call: dict,
        agent_configs: dict,
        policy: RunPolicy,
        *,
        title_prefix: str,
    ) -> AgentTask:
        agent_id = str(call.get("agent_id") or "")
        definition = workflow_definition(agent_id)
        instructions = (
            f"大脑专项指令：{call.get('question')}（原因：{call.get('reason')}）。"
            "职责内完成分析；不得补全缺失事实。"
        )
        custom = self.repository.agent_custom_instructions(agent_id)
        if custom:
            instructions += "\n本项目附加要求：" + custom
        return AgentTask(
            run_id=run_id,
            agent_id=agent_id,
            title=title_prefix + " · " + self._task_title(agent_id),
            instructions=instructions,
            symbols=[str(item) for item in call.get("symbols") or []][:3],
            config_version=int((agent_configs.get(agent_id) or {}).get("config_version") or 1),
            prompt_version=self._prompt(
                AGENT_PROMPT_IDS[agent_id],
                RISK_PROMPT if agent_id == "risk" else AGENT_PROMPTS.get(agent_id, ""),
            )[1],
            workflow_id=str(definition["workflow_id"]),
            workflow_version=int(definition["version"]),
        )

    def _execute_brain_calls(
        self,
        run_id: str,
        report: ParsedReport,
        tasks: list[AgentTask],
        contributions: list[AgentContribution],
        control: RunControl,
        policy: RunPolicy,
        state: ResearchState,
        fired_agents: set[str],
        fired_scopes: dict[str, set[str]],
    ) -> list[AgentContribution]:
        """执行大脑点名的专项调查（顺序执行；预算与每 Agent ≤1 次约束同事件规则。

        若规则已触发过该 Agent，大脑的请求按覆盖度处理：只补查尚未覆盖的标的。
        """
        for task in tasks:
            control.wait_if_paused()
            if control.cancelled.is_set():
                return contributions
            agent_id = task.agent_id
            covered = fired_scopes.get(agent_id, set())
            blocked = ""
            if agent_id in fired_agents and covered == {"*"}:
                blocked = "该 Agent 已覆盖全市场"
            elif agent_id in fired_agents:
                uncovered = sorted(set(task.symbols) - covered)
                if not uncovered:
                    blocked = "目标标的已由本轮调查覆盖"
                else:
                    task = task.model_copy(update={"symbols": uncovered})
            elif len(fired_agents) >= policy.max_event_agent_calls:
                blocked = "事件预算已用尽"
            elif agent_id not in self.repository.enabled_agent_ids():
                blocked = "Agent 已停用"
            if blocked:
                state.trigger_log.append(
                    TriggerRecord(
                        rule_id="brain.call." + agent_id,
                        agent_id=agent_id,
                        fired_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        round=state.round,
                        condition_summary=f"大脑点名 {_agent_display_name(agent_id)}（未派发：{blocked}）",
                    )
                )
                continue
            fired_agents.add(agent_id)
            self._emit(
                run_id,
                "task.brain_dispatch",
                agent_id="coordinator",
                payload={"tasks": [task.model_dump(mode="json")], "requested_by": "brain"},
            )
            self._emit(
                run_id,
                "agent.lifecycle",
                agent_id=agent_id,
                payload={"status": "started", "stage": "brain_dispatch"},
            )
            result = self._execute_task(task, report, control)
            contributions.append(result)
            self._emit(
                run_id,
                "agent.message",
                agent_id=result.agent_id,
                payload={
                    **result.model_dump(mode="json"),
                    "stage": "brain_dispatch_result",
                    "requested_by": "brain",
                },
            )
            if agent_id in ("global_sector_flow", "sector_transmission"):
                fired_scopes[agent_id] = {"*"}
            else:
                fired_scopes[agent_id] = (fired_scopes.get(agent_id) or set()) | set(task.symbols)
            chain = self._event_dispatch(
                run_id,
                f"after_agent:{agent_id}",
                report,
                contributions,
                None,
                control,
                policy,
                state,
                fired_agents,
                fired_scopes,
            )
            contributions.extend(chain)
        return contributions

    def _neck_compress(
        self,
        contributions: list[AgentContribution],
        review: AgentContribution,
        state: ResearchState,
        *,
        round: int,
    ) -> ResearchState:
        """颈部压缩：把各 Agent 结果收敛为 ResearchState（lanes 整体替换）。"""
        lanes: list[LaneSummary] = []
        for contribution in _latest_contributions_by_agent(contributions):
            claims = [
                claim.text
                for claim in contribution.claims
                if claim.kind in ("fact", "interpretation")
            ]
            lanes.append(
                LaneSummary(
                    agent_id=contribution.agent_id,
                    lane=_PROFILE_LANE.get(contribution.agent_id, contribution.agent_id),
                    headline=str(contribution.summary or "").replace("\n", " ")[:160],
                    key_findings=[text[:200] for text in claims[:5]],
                    risks=list(contribution.risks)[:5],
                    unknowns=list(contribution.unknowns)[:5],
                    structured=_compact_structured(contribution.structured_data),
                )
            )
        state.round = round
        state.lanes = lanes
        return state

    def _emit_research_state(self, run_id: str, state: ResearchState) -> None:
        self._emit(
            run_id,
            "research.state",
            agent_id="coordinator",
            payload=state.model_dump(mode="json"),
        )

    def _event_dispatch(
        self,
        run_id: str,
        checkpoint: str,
        report: ParsedReport,
        contributions: list[AgentContribution],
        review: AgentContribution | None,
        control: RunControl,
        policy: RunPolicy,
        state: ResearchState,
        fired_agents: set[str],
        fired_scopes: dict[str, set[str]],
    ) -> list[AgentContribution]:
        """事件规则求值与派单：扳机由代码判定，模型只写任务书内容。

        未实现工作流的触发 Agent 只记录不派单（trigger_log.dispatched=False）。
        """
        context = self._build_rule_context(report, contributions, review, state)
        records = EventRuleEngine().evaluate(checkpoint, context)
        added: list[AgentContribution] = []
        for record in records:
            blocked_reason = ""
            if record.agent_id not in self._task_handlers():
                blocked_reason = "工作流未实现"
            elif record.agent_id in fired_agents:
                blocked_reason = "本轮已派发过"
            elif len(fired_agents) >= policy.max_event_agent_calls:
                blocked_reason = "事件预算已用尽"
            elif record.agent_id not in self.repository.enabled_agent_ids():
                blocked_reason = "Agent 已停用"
            if blocked_reason:
                state.trigger_log.append(
                    record.model_copy(
                        update={
                            "dispatched": False,
                            "condition_summary": record.condition_summary + f"（未派发：{blocked_reason}）",
                        }
                    )
                )
                continue
            state.trigger_log.append(record)
            self._emit_research_state(run_id, state)
            fired_agents.add(record.agent_id)
            fired_scopes[record.agent_id] = (
                fired_scopes.get(record.agent_id, set()) | _event_scope_for(record, report)
            )
            task = self._event_task(run_id, report, record, policy)
            self._emit(
                run_id,
                "task.event_dispatch",
                agent_id="coordinator",
                payload={
                    "rule_id": record.rule_id,
                    "checkpoint": checkpoint,
                    "tasks": [task.model_dump(mode="json")],
                },
            )
            self._emit(
                run_id,
                "agent.lifecycle",
                agent_id=task.agent_id,
                payload={"status": "started", "stage": "event_dispatch"},
            )
            result = self._execute_task(task, report, control)
            added.append(result)
            self._emit(
                run_id,
                "agent.message",
                agent_id=result.agent_id,
                payload={
                    **result.model_dump(mode="json"),
                    "stage": "event_dispatch_result",
                    "triggered_by": record.rule_id,
                },
            )
            chain = self._event_dispatch(
                run_id,
                f"after_agent:{record.agent_id}",
                report,
                [*contributions, *added],
                review,
                control,
                policy,
                state,
                fired_agents,
                fired_scopes,
            )
            added.extend(chain)
        if records:
            self._emit_research_state(run_id, state)
        return added

    def _event_task(
        self,
        run_id: str,
        report: ParsedReport,
        record: TriggerRecord,
        policy: RunPolicy,
    ) -> AgentTask:
        agent_id = record.agent_id
        agent_configs = {item["agent_id"]: item for item in self.repository.list_agent_configs()}
        definition = workflow_definition(agent_id)
        instructions = (
            f"事件触发（{record.rule_id}）：{record.condition_summary}。"
            "触发器数据：" + json.dumps(record.inputs, ensure_ascii=False, default=str) + "。"
            "职责内完成分析；不得补全缺失事实。"
        )
        custom = self.repository.agent_custom_instructions(agent_id)
        if custom:
            instructions += "\n本项目附加要求：" + custom
        return AgentTask(
            run_id=run_id,
            agent_id=agent_id,
            title=self._task_title(agent_id) + "（事件触发）",
            instructions=instructions,
            symbols=[row.symbol for row in report.stocks[: policy.max_external_symbols]],
            config_version=int((agent_configs.get(agent_id) or {}).get("config_version") or 1),
            prompt_version=self._prompt(AGENT_PROMPT_IDS[agent_id], AGENT_PROMPTS[agent_id])[1],
            workflow_id=str(definition["workflow_id"]),
            workflow_version=int(definition["version"]),
        )

    def _build_rule_context(
        self,
        report: ParsedReport,
        contributions: list[AgentContribution],
        review: AgentContribution | None,
        state: ResearchState,
    ) -> RuleContext:
        rows = []
        for row in report.stocks:
            rows.append(
                {
                    "symbol": row.symbol,
                    "super_large_anomaly": row.super_large_anomaly,
                    "realtime_formula_wanyuan": (
                        float(row.realtime_formula_wanyuan)
                        if row.realtime_formula_wanyuan is not None
                        else None
                    ),
                    "effective_threshold": _row_effective_threshold(row),
                }
            )
        global_snapshot: dict = {}
        for contribution in _latest_contributions_by_agent(contributions):
            if contribution.agent_id == "global_market":
                global_snapshot = contribution.structured_data
        market_indices = tuple(global_snapshot.get("market_indices") or ())
        market_status = str(global_snapshot.get("status") or "")
        risk_categories: list[str] = []
        if review is not None:
            risk_categories = list(review.structured_data.get("risk_categories") or [])
            for text in review.risks:
                category = _risk_category(text)
                if category != "潜在利空" and category not in risk_categories:
                    risk_categories.append(category)
        chained_structured: dict[str, dict] = {}
        for contribution in _latest_contributions_by_agent(contributions):
            if contribution.agent_id == "global_sector_flow":
                chained_structured["global_sector_flow.move"] = contribution.structured_data
        chained_fired = frozenset(
            record.rule_id for record in state.trigger_log if record.dispatched
        )
        return RuleContext(
            rows=tuple(rows),
            market_status=market_status,
            market_indices=market_indices,
            risk_categories=tuple(risk_categories),
            chained_fired=chained_fired,
            chained_structured=chained_structured,
            round=state.round,
        )

    def _task_handlers(self) -> dict[str, Callable[[AgentTask, ParsedReport], AgentContribution]]:
        """已实现专用工作流的 Agent → 执行器；未登记的工作流走通用确定性执行器。"""
        return {
            "quant_signal": self._run_quant_workflow,
            "company_industry": lambda task, report: self._llm_contribution(
                task,
                report,
                self._company_contribution(report, task.instructions, task.symbols),
            ),
            "global_market": self._run_global_market_workflow,
            "capital_trace": self._run_capital_trace_workflow,
            "bearish_analysis": self._run_bearish_analysis_workflow,
            "global_sector_flow": self._run_global_sector_flow_workflow,
            "sector_transmission": self._run_sector_transmission_workflow,
        }

    def _generic_contribution(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        """未登记专用工作流的 Agent 的确定性兜底：只说明职责边界，不编造事实。"""
        profile = _PROFILE_BY_ID.get(task.agent_id)
        description = profile.description if profile else ""
        return AgentContribution(
            agent_id=task.agent_id,
            summary=(
                f"{_agent_display_name(task.agent_id)}完成职责范围内的确定性分析：{description}"
                "当前运行未取得可进一步核验的外部数据。"
            ),
            unknowns=[
                f"{_agent_display_name(task.agent_id)}未登记专用数据工作流，无法取得额外证据。"
            ],
        )

    def _execute_task(self, task: AgentTask, report: ParsedReport, control: RunControl) -> AgentContribution:
        control.wait_if_paused()
        if control.cancelled.is_set():
            return AgentContribution(agent_id=task.agent_id, summary="任务已取消")
        started = time.perf_counter()
        self._emit(
            task.run_id,
            "agent.lifecycle",
            agent_id=task.agent_id,
            payload={"status": "started", "stage": "specialist"},
        )
        try:
            handler = self._task_handlers().get(task.agent_id)
            if handler is not None:
                contribution = handler(task, report)
            else:
                contribution = self._llm_contribution(
                    task,
                    report,
                    self._generic_contribution(task, report),
                )
            self._emit(
                task.run_id,
                "agent.lifecycle",
                agent_id=task.agent_id,
                payload={
                    "status": "completed",
                    "stage": "specialist",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "evidence_count": len(contribution.evidence),
                    "risk_count": len(contribution.risks),
                },
            )
            return contribution
        except Exception as exc:
            self._emit(
                task.run_id,
                "agent.lifecycle",
                agent_id=task.agent_id,
                payload={
                    "status": "failed",
                    "stage": "specialist",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise

    def _hydrate_report_security_names(self, report: ParsedReport) -> None:
        profiles = self.repository.security_profiles([stock.symbol for stock in report.stocks])
        for stock in report.stocks:
            profile = profiles.get(stock.symbol.upper()) or {}
            stable_name = str(profile.get("name") or "").strip()
            if stable_name:
                stock.name = stable_name

    def _run_quant_workflow(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        self._emit_workflow_plan(task)
        observed_at = report.generated_at or report.run_slot or task.run_id
        self._workflow_step(
            task,
            "identity_sync",
            lambda: self.repository.sync_security_master(report.stocks, observed_at),
        )
        deterministic = self._workflow_step(
            task,
            "signal_rules",
            lambda: self._quant_contribution(report, task.run_id),
        )
        self._workflow_step(
            task,
            "visual_payload",
            lambda: deterministic.structured_data.get("flow_structure") or [],
        )
        stability = self._workflow_step(
            task,
            "stability_memory",
            lambda: {
                stock.symbol: self.repository.signal_stability(stock.symbol)
                for stock in report.stocks[:5]
            },
        )
        deterministic.follow_up_requests.append(
            "稳定性记忆已更新："
            + "；".join(
                f"{symbol} {value['sample_count']} 次样本"
                for symbol, value in stability.items()
            )
        )
        return self._workflow_step(
            task,
            "plain_explanation",
            lambda: self._llm_contribution(task, report, deterministic),
        )

    def _run_global_market_workflow(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        self._emit_workflow_plan(task)
        self._workflow_step(
            task,
            "session_scope",
            lambda: "每个指数使用自身最近一个有效收盘日，不按北京时间强行对齐",
        )
        snapshot = self._workflow_step(
            task,
            "index_fetch",
            lambda: self.global_market_client.snapshot(
                report.generated_at or report.report_date or report.run_slot
            ),
        )
        self._workflow_step(
            task,
            "normalize_returns",
            lambda: len(snapshot.get("market_indices") or []),
        )
        contribution = self._workflow_step(
            task,
            "visual_payload",
            lambda: self._global_market_contribution(snapshot),
        )
        return self._workflow_step(
            task,
            "market_explanation",
            lambda: self._llm_contribution(task, report, contribution),
        )

    def _run_capital_trace_workflow(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        self._emit_workflow_plan(task)
        inputs = _event_inputs(task)
        restrict = task.symbols if task.instructions.startswith("大脑专项指令") else None
        trigger_rows = self._workflow_step(
            task,
            "trigger_scope",
            lambda: _capital_trigger_rows(report, inputs, restrict),
        )
        history = self._workflow_step(
            task,
            "flow_history",
            lambda: self._capital_trace_history(trigger_rows, report),
        )
        classified = self._workflow_step(
            task,
            "pulse_classify",
            lambda: _classify_capital_pulses(history),
        )
        fallback = self._capital_trace_fallback(task, trigger_rows, classified)
        return self._workflow_step(
            task,
            "capital_explanation",
            lambda: self._llm_contribution(task, report, fallback),
        )

    def _capital_trace_history(self, entries: list[dict], report: ParsedReport) -> dict:
        """逐只查询近 10 日资金流历史：Tushare moneyflow 为主，东财个股资金流为备。"""
        end = _normalized_date(report.report_date) or _normalized_date(report.generated_at)
        try:
            end_date = datetime.strptime(end, "%Y-%m-%d") if end else datetime.now()
        except ValueError:
            end_date = datetime.now()
        start_ymd = (end_date - timedelta(days=14)).strftime("%Y%m%d")
        end_ymd = end_date.strftime("%Y%m%d")
        result: dict = {}
        for entry in entries:
            symbol = entry["symbol"]
            rows: list[dict] = []
            errors: list[str] = []
            if self.tushare_client is not None:
                try:
                    raw = self.tushare_client.query(
                        "moneyflow",
                        params={
                            "ts_code": normalize_ts_code(symbol),
                            "start_date": start_ymd,
                            "end_date": end_ymd,
                        },
                        fields="ts_code,trade_date,net_mf_amount",
                    )
                    rows = [
                        {"date": str(item.get("trade_date") or ""), "net": float(item.get("net_mf_amount") or 0)}
                        for item in raw
                        if item.get("trade_date")
                    ]
                except Exception as exc:
                    errors.append("Tushare 资金流：" + _external_error(exc))
            if not rows and self.public_a_stock_client is not None:
                try:
                    code = symbol.split(".", 1)[0]
                    daily = self.public_a_stock_client.stock_fund_flow_history(code, days=10)
                    rows = [
                        {"date": str(item.get("date") or ""), "net": float(item.get("main_net") or 0)}
                        for item in daily
                        if item.get("date") and str(item["date"]) <= end
                    ]
                except Exception as exc:
                    errors.append("东财资金流：" + _external_error(exc))
            rows.sort(key=lambda item: str(item["date"]))
            result[symbol] = {"rows": rows[-10:], "errors": errors, "trigger": entry}
        return result

    def _capital_trace_fallback(
        self,
        task: AgentTask,
        trigger_rows: list[dict],
        classified: dict,
    ) -> AgentContribution:
        labels = {
            "single_day_pulse": "单日脉冲：当日异动缺乏历史延续",
            "persistent_inflow": "持续流入：最近 5 日中至少 3 日主力净流入为正",
            "persistent_outflow": "持续流出：最近 5 日中至少 3 日主力净流出为正",
            "insufficient_data": "资金历史数据不足，无法判断持续性",
        }
        lines: list[str] = []
        unknowns: list[str] = []
        evidence: list[EvidenceItem] = []
        per_symbol: list[dict] = []
        for entry in trigger_rows:
            symbol = entry["symbol"]
            item = classified.get(symbol) or {}
            rows = item.get("rows") or []
            label = labels.get(str(item.get("classification")), "无法判断")
            trigger_text = (
                "超大单异常=True"
                if entry.get("super_large_anomaly")
                else f"资金公式 {entry.get('realtime_formula_wanyuan')} 万 ≥ 2×门槛 {entry.get('effective_threshold')} 万"
            )
            series = (
                "、".join(f"{str(row['date'])[-5:]}:{float(row['net']):+.0f}万" for row in rows[-8:])
                or "无"
            )
            lines.append(
                f"{symbol}｜{entry.get('name') or '未知名称'}：触发原因：{trigger_text}。"
                f"近 10 日主力净额序列：{series}。分类结论：{label}。"
            )
            for error in item.get("errors") or []:
                unknowns.append(f"{symbol} 资金历史部分不可用：{error}")
            if not rows:
                unknowns.append(f"{symbol} 资金历史不可用：无 Tushare/东财数据源或查询失败")
            if rows:
                evidence_item = EvidenceItem(
                    source_type="tushare",
                    title=f"{symbol} 资金流历史（{len(rows)} 日）",
                    excerpt=series,
                    published_at=str(rows[-1].get("date") or ""),
                    symbols=[symbol],
                )
                evidence.append(evidence_item)
            per_symbol.append(
                {
                    "symbol": symbol,
                    "days": len(rows),
                    "today_net": item.get("today_net"),
                    "median_5d": item.get("median_5d"),
                    "classification": item.get("classification"),
                }
            )
        rule_id = task.instructions.split("）", 1)[0].removeprefix("事件触发（")
        return AgentContribution(
            agent_id="capital_trace",
            summary="\n".join(lines) or "资金追查：未定位到资金异常标的。",
            evidence=evidence,
            unknowns=unknowns,
            structured_data={"per_symbol": per_symbol, "rule_id": rule_id},
        )

    def _run_bearish_analysis_workflow(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        self._emit_workflow_plan(task)
        inputs = _event_inputs(task)
        categories = list(inputs.get("risk_categories") or [])
        scope = self._workflow_step(
            task,
            "risk_scope",
            lambda: [
                item
                for item in _risk_search_scope(report, [], limit=5)
                if item["symbol"] in task.symbols
            ],
        )
        searched = self._workflow_step(
            task,
            "targeted_search",
            lambda: self._search_negative_news(
                report,
                scope,
                focus="事件触发：" + ("、".join(categories) if categories else "关键利空类别"),
            ),
        )
        classified = self._workflow_step(
            task,
            "duration_rubric",
            lambda: _classify_bearish_duration(searched),
        )
        fallback = self._bearish_fallback(task, report, classified, categories)
        return self._workflow_step(
            task,
            "bearish_explanation",
            lambda: self._llm_contribution(task, report, fallback),
        )

    def _bearish_fallback(
        self,
        task: AgentTask,
        report: ParsedReport,
        classified: dict,
        categories: list[str],
    ) -> AgentContribution:
        evidence: list[EvidenceItem] = list(classified.get("evidence") or [])
        lines: list[str] = []
        per_symbol: dict = {}
        for symbol in {row.symbol for row in report.stocks}:
            items = [item for item in evidence if symbol in item.symbols]
            structural = [
                item
                for item in items
                if _bearish_duration(str(item.title) + " " + str(item.excerpt)) == "structural"
            ]
            event_driven = [
                item
                for item in items
                if _bearish_duration(str(item.title) + " " + str(item.excerpt)) == "event_driven"
            ]
            per_symbol[symbol] = {
                "structural": [
                    {"title": item.title, "published_at": item.published_at} for item in structural[:4]
                ],
                "event_driven": [
                    {"title": item.title, "published_at": item.published_at} for item in event_driven[:4]
                ],
            }
            if structural:
                lines.append(
                    f"{symbol}：结构性利空（长期影响公司治理、经营或再融资）——"
                    + "；".join(item.title for item in structural[:3])
                    + "。"
                )
            if event_driven:
                lines.append(
                    f"{symbol}：事件性利空（短期压力取决于触发节奏）——"
                    + "；".join(item.title for item in event_driven[:3])
                    + "。"
                )
        unknowns = list(classified.get("unknowns") or [])
        rule_id = task.instructions.split("）", 1)[0].removeprefix("事件触发（")
        return AgentContribution(
            agent_id="bearish_analysis",
            summary="\n".join(lines) or "利空分析：未检索到可分类的利空事件。",
            evidence=evidence,
            unknowns=unknowns,
            structured_data={
                "per_symbol": per_symbol,
                "trigger_categories": categories,
                "rule_id": rule_id,
            },
        )

    def _run_global_sector_flow_workflow(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        self._emit_workflow_plan(task)
        self._workflow_step(
            task,
            "session_scope",
            lambda: "行业 ETF 取当地交易日严格早于 A 股报告日的最近收盘",
        )
        snapshot = self._workflow_step(
            task,
            "sector_fetch",
            lambda: self.global_market_client.sector_snapshot(
                report.generated_at or report.report_date or report.run_slot
            ),
        )
        anomaly = self._workflow_step(
            task,
            "anomaly_sort",
            lambda: _anomaly_sectors(snapshot),
        )
        contribution = self._global_sector_contribution(snapshot, anomaly)
        return self._workflow_step(
            task,
            "sector_explanation",
            lambda: self._llm_contribution(task, report, contribution),
        )

    def _global_sector_contribution(self, snapshot: dict, anomaly: list[dict]) -> AgentContribution:
        sectors = list(snapshot.get("sectors") or [])
        status = str(snapshot.get("status") or "unavailable")
        lines = [str(snapshot.get("notice") or "外围行业 ETF 数据不可用。")]
        if sectors:
            lines.append(
                "行业 ETF 走势："
                + "；".join(
                    f"{item['name']} {float(item.get('change_percent') or 0):+.2f}%"
                    for item in sectors[:10]
                )
                + "。"
            )
        if anomaly:
            lines.append(
                "异常板块（|涨跌幅|≥2%）："
                + "；".join(
                    f"{item['name']} {item['direction']} {abs(item['change_percent']):.2f}%"
                    for item in anomaly
                )
                + "。"
            )
        else:
            lines.append("本轮行业 ETF 未出现达到阈值的异常板块。")
        evidence: list[EvidenceItem] = []
        if status == "live_delayed":
            for item in sectors[:10]:
                evidence.append(
                    EvidenceItem(
                        source_type="market_data",
                        title=f"{item['name']} 行业 ETF 延迟行情",
                        excerpt=(
                            f"最近收盘 {item.get('close')}，"
                            f"涨跌幅 {float(item.get('change_percent') or 0):+.2f}%。"
                        ),
                        url=str(item.get("source_url") or ""),
                        published_at=str(item.get("trade_date") or ""),
                        symbols=[str(item.get("ticker") or "")],
                    )
                )
        unknowns: list[str] = []
        if status == "demo_fallback":
            unknowns.append("外围行业 ETF 行情接口不可用，当前为演示占位数据，不触发板块传导链")
        unknowns.extend(str(item) for item in snapshot.get("errors") or [])
        return AgentContribution(
            agent_id="global_sector_flow",
            summary="\n".join(lines),
            evidence=evidence,
            unknowns=unknowns,
            structured_data={
                "sectors": sectors,
                "status": status,
                "anomaly_sectors": anomaly,
                "notice": str(snapshot.get("notice") or ""),
                "errors": list(snapshot.get("errors") or []),
            },
        )

    def _run_sector_transmission_workflow(self, task: AgentTask, report: ParsedReport) -> AgentContribution:
        self._emit_workflow_plan(task)
        inputs = _event_inputs(task)
        anomaly = list(inputs.get("anomaly_sectors") or [])
        resolved = self._workflow_step(
            task,
            "mapping_resolve",
            lambda: _resolve_transmissions(anomaly),
        )
        boards_payload = self._workflow_step(
            task,
            "board_fetch",
            lambda: self._fetch_industry_boards(),
        )
        ranked = self._workflow_step(
            task,
            "transmission_rank",
            lambda: _rank_transmissions(resolved, boards_payload),
        )
        fallback = self._transmission_fallback(ranked, boards_payload)
        return self._workflow_step(
            task,
            "transmission_explanation",
            lambda: self._llm_contribution(task, report, fallback),
        )

    def _fetch_industry_boards(self) -> dict:
        if self.public_a_stock_client is None:
            return {"boards": [], "error": "未配置公共行情客户端"}
        try:
            return {"boards": self.public_a_stock_client.industry_board_quotes(limit=100), "error": ""}
        except Exception as exc:
            return {"boards": [], "error": _external_error(exc)}

    def _transmission_fallback(self, ranked: list[dict], payload: dict) -> AgentContribution:
        lines: list[str] = []
        unknowns: list[str] = []
        error = str(payload.get("error") or "")
        if error:
            unknowns.append(f"A 股板块行情不可用：{error}")
        for item in ranked:
            boards = item["a_share_boards"]
            board_text = (
                "；".join(
                    f"{board['name']}({board['change_percent']:+.2f}%→{board['direction']})"
                    for board in boards
                )
                or "暂无匹配的 A 股板块行情"
            )
            lines.append(
                f"{item['foreign_ticker']} {item['foreign_name']} {item['foreign_change_percent']:+.2f}% "
                f"→ A 股映射板块：{board_text}。"
            )
        if not lines:
            lines.append("板块传导映射：本轮没有可映射的异常板块。")
        return AgentContribution(
            agent_id="sector_transmission",
            summary="\n".join(lines),
            unknowns=unknowns,
            structured_data={"transmissions": ranked},
        )

    def _emit_workflow_plan(self, task: AgentTask) -> None:
        definition = workflow_definition(task.agent_id)
        self._emit(
            task.run_id,
            "workflow.plan",
            agent_id=task.agent_id,
            payload={
                "workflow_id": definition["workflow_id"],
                "workflow_version": definition["version"],
                "mode": definition["mode"],
                "description": definition["description"],
                "nodes": definition["nodes"],
            },
        )

    def _workflow_step(self, task: AgentTask, node_id: str, action: Callable[[], object]):
        started = time.perf_counter()
        self._emit(
            task.run_id,
            "workflow.node",
            agent_id=task.agent_id,
            payload={"node_id": node_id, "status": "started", "workflow_id": task.workflow_id},
        )
        try:
            result = action()
            self._emit(
                task.run_id,
                "workflow.node",
                agent_id=task.agent_id,
                payload={
                    "node_id": node_id,
                    "status": "completed",
                    "workflow_id": task.workflow_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return result
        except Exception as exc:
            self._emit(
                task.run_id,
                "workflow.node",
                agent_id=task.agent_id,
                payload={
                    "node_id": node_id,
                    "status": "failed",
                    "workflow_id": task.workflow_id,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise

    def _quant_contribution(self, report: ParsedReport, run_id: str = "") -> AgentContribution:
        report_evidence = EvidenceItem(
            source_type="report",
            title=f"PTrade 结构化报告 · {report.generated_at or report.run_slot or report.report_id}",
            excerpt=f"selected {len(report.selected_rows)} 只，near {len(report.near_rows)} 只，解析状态 {report.parse_status}。",
            symbols=[row.symbol for row in report.stocks],
        )
        evidence = [report_evidence]
        stable_profiles = self.repository.security_profiles([row.symbol for row in report.stocks])
        if stable_profiles:
            stable_evidence = EvidenceItem(
                source_type="local_stable_master",
                title="本地稳定证券代码、名称与行业映射",
                excerpt="；".join(
                    f"{symbol} {profile.get('name') or '名称缺失'} {profile.get('industry') or '行业缺失'}"
                    for symbol, profile in stable_profiles.items()
                ),
                symbols=list(stable_profiles),
            )
            evidence.append(stable_evidence)
        evidence_ids = [item.evidence_id for item in evidence]
        formal = _formal_recommendation_rows(report)
        candidates: list[tuple[int, Decimal, ReportStock, list[str]]] = []
        avoided = 0
        condition_names = {
            "funding": "实时资金未过门槛",
            "volume_ratio": "量比不在 1.1 到 2.5",
            "turnover": "换手率不在 1% 到 10%",
            "buy_sell": "外盘没有大于内盘",
            "structure": "超大单与大单方向相反",
        }
        for row in report.near_rows:
            missing = [
                label
                for label, value in (
                    ("实时资金", row.realtime_formula_wanyuan),
                    ("资金门槛", row.flow_threshold_wanyuan),
                    ("量比", row.vol_ratio),
                    ("换手率", row.turnover_now_pct),
                    ("外盘内盘", row.l4_buy_sell),
                )
                if value is None
            ]
            if missing:
                avoided += 1
                continue
            formula = row.realtime_formula_wanyuan or Decimal("0")
            threshold = _row_effective_threshold(row)
            funding_ok = formula >= threshold
            failures = []
            if not funding_ok:
                failures.append("funding")
            if not (Decimal("1.1") <= (row.vol_ratio or Decimal("0")) <= Decimal("2.5")):
                failures.append("volume_ratio")
            if not (Decimal("1") <= (row.turnover_now_pct or Decimal("0")) <= Decimal("10")):
                failures.append("turnover")
            if row.l4_buy_sell is not True:
                failures.append("buy_sell")
            if row.super_large_anomaly is True:
                failures.append("structure")
            other_failures = [item for item in failures if item != "funding"]
            gap = max(Decimal("0"), threshold - formula)
            priority: int | None = None
            if row.super_large_anomaly is True or formula < 0:
                priority = None
            elif funding_ok and len(other_failures) == 1:
                priority = 1
            elif not funding_ok and gap < Decimal("500") and not other_failures:
                priority = 2
            elif not funding_ok and gap <= Decimal("1000") and len(other_failures) <= 1:
                priority = 3
            if priority is None:
                avoided += 1
            else:
                candidates.append((priority, gap, row, [condition_names[item] for item in failures]))
        candidates.sort(
            key=lambda item: (
                item[0],
                -(item[2].realtime_formula_wanyuan - _row_effective_threshold(item[2]))
                if item[0] == 1
                else item[1],
                -(item[2].super_net_wanyuan or Decimal("0")),
                item[2].symbol,
            )
        )
        candidates = candidates[: max(0, 5 - len(formal))]

        levels: dict[str, tuple[str, int | None]] = {
            row.symbol: ("formal", index)
            for index, row in enumerate(formal, 1)
        }
        levels.update(
            {
                row.symbol: (f"candidate_p{priority}", index)
                for index, (priority, _gap, row, _failures) in enumerate(candidates, 1)
            }
        )
        if run_id:
            self.repository.record_signal_observations(
                run_id,
                report,
                levels,
                rule_version="ptrade-signal-v1",
            )

        time_label = report.generated_at or report.run_slot or "未识别时间"
        lines = [
            f"这份报告生成于 {time_label}。共看到 {len(report.selected_rows)} 只全部条件通过、{len(report.near_rows)} 只接近通过。",
        ]
        claims: list[Claim] = []
        if formal:
            lines.append(f"\n正式观察（{len(formal)} 只，按超大单净额优先排序）：")
            for index, row in enumerate(formal, 1):
                line = _quant_stock_line(row, index=index, pool="正式观察")
                if run_id:
                    line += _stability_note(self.repository.signal_stability(row.symbol))
                lines.append(line)
                claims.append(
                    Claim(
                        text=line,
                        kind="fact",
                        symbols=[row.symbol],
                        evidence_ids=evidence_ids,
                        confidence="high",
                    )
                )
        else:
            lines.append("\n正式观察：目前没有全部核心条件都通过的标的。")
        if candidates:
            lines.append(f"\n候选观察（{len(candidates)} 只）：")
            for index, (priority, gap, row, failures) in enumerate(candidates, 1):
                surplus = (row.realtime_formula_wanyuan or Decimal("0")) - _row_effective_threshold(row)
                reason = {
                    1: f"资金已超过门槛 {_number(abs(surplus))} 万元，但还差：{'、'.join(failures)}",
                    2: f"其他盘口条件都通过，资金还差 {_number(gap)} 万元",
                    3: f"资金还差 {_number(gap)} 万元，同时最多只差一个盘口条件：{'、'.join(failures) or '无'}",
                }[priority]
                line = f"{index}. {_security_label(row)}（第 {priority} 优先级）：{reason}。" + _quant_metrics(row)
                if run_id:
                    line += _stability_note(self.repository.signal_stability(row.symbol))
                lines.append(line)
                claims.append(
                    Claim(
                        text=line,
                        kind="interpretation",
                        symbols=[row.symbol],
                        evidence_ids=evidence_ids,
                        confidence="high",
                    )
                )
        else:
            lines.append("\n候选观察：没有符合三档候选规则的标的。")
        if avoided:
            lines.append(f"\n另外有 {avoided} 只 near 标的因为资金过弱、失败条件过多、结构异常或字段缺失，没有列入候选观察。")
        risks = []
        missing_count = sum(1 for row in report.stocks if row.missing_fields)
        anomaly_symbols = [row.symbol for row in report.stocks if row.super_large_anomaly is True]
        if report.parse_status != "valid":
            risks.append("报告解析不完整，所有排序只能作为低置信度观察")
        if missing_count:
            risks.append(f"{missing_count} 行存在核心字段缺失")
        if anomaly_symbols:
            risks.append("这些标的存在超大单与大单方向相反：" + "、".join(anomaly_symbols))
        flow_structure = [
            {
                "symbol": row.symbol,
                "name": str(row.name or ""),
                "level": "formal",
                "super_net_wanyuan": _flow_net(row.super_net_wanyuan),
                "large_net_wanyuan": _flow_net(row.large_net_wanyuan),
                "medium_net_wanyuan": _flow_net(row.medium_net_wanyuan),
                "small_net_wanyuan": _flow_net(row.small_net_wanyuan),
            }
            for row in formal
        ]
        flow_structure.extend(
            {
                "symbol": row.symbol,
                "name": str(row.name or ""),
                "level": f"candidate_p{priority}",
                "super_net_wanyuan": _flow_net(row.super_net_wanyuan),
                "large_net_wanyuan": _flow_net(row.large_net_wanyuan),
                "medium_net_wanyuan": _flow_net(row.medium_net_wanyuan),
                "small_net_wanyuan": _flow_net(row.small_net_wanyuan),
            }
            for priority, _gap, row, _failures in candidates
        )
        return AgentContribution(
            agent_id="quant_signal",
            summary="\n".join(lines),
            claims=claims,
            evidence=evidence,
            risks=risks,
            structured_data={"flow_structure": flow_structure},
        )

    def _company_contribution(
        self,
        report: ParsedReport,
        focus: str = "",
        focus_symbols: list[str] | None = None,
    ) -> AgentContribution:
        evidence: list[EvidenceItem] = []
        claims: list[Claim] = []
        unknowns: list[str] = []
        lines: list[str] = []
        industries: list[str] = []
        snapshots: dict[str, dict] = {}
        company_stocks = (report.selected_rows or report.stocks)[:3]
        stable_profiles = self.repository.security_profiles(
            [stock.symbol for stock in company_stocks]
        )
        if self.tushare_client is not None:
            with ThreadPoolExecutor(max_workers=max(1, min(3, len(company_stocks)))) as executor:
                pending = {
                    executor.submit(self.tushare_client.company_snapshot, stock.symbol): stock.symbol
                    for stock in company_stocks
                }
                for future in as_completed(pending):
                    symbol = pending[future]
                    try:
                        snapshots[symbol] = future.result()
                    except Exception as exc:
                        unknowns.append(f"{symbol} Tushare 查询失败：" + _external_error(exc))
        else:
            unknowns.append("Tushare 未配置，公司财务和估值字段无法查询")

        preferred_symbols = [row.symbol for row in company_stocks]
        public_bundles = (
            self.public_a_stock_client.research(preferred_symbols, max_stocks=3)
            if self.public_a_stock_client is not None
            else []
        )
        public_by_symbol = {str(item.get("symbol")): item for item in public_bundles}

        for stock in company_stocks:
            result = _company_stock_details(
                stock,
                snapshots.get(stock.symbol) or {},
                public_by_symbol.get(stock.symbol) or {},
                stable_profiles.get(stock.symbol.upper()) or {},
            )
            evidence.extend(result["evidence"])
            unknowns.extend(result["unknowns"])
            lines.append(result["text"])
            if result["industry"] and result["industry"] not in industries:
                industries.append(result["industry"])
            claims.append(
                Claim(
                    text=result["claim"],
                    kind="fact" if result["evidence_ids"] else "limitation",
                    symbols=[stock.symbol],
                    evidence_ids=result["evidence_ids"],
                    confidence="high" if result["evidence_ids"] else "low",
                )
            )

        if industries and self.tavily_client is not None:
            try:
                response = self.tavily_client.search(
                    "A股 " + " ".join(industries[:3]) + " 行业景气度 产业政策 供需变化 风险 近一个月",
                    max_results=5,
                )
                industry_evidence = _tavily_evidence(
                    response,
                    symbols=[stock.symbol for stock in company_stocks],
                )
                evidence.extend(industry_evidence)
                if industry_evidence:
                    lines.append(
                        f"行业补充：围绕 {'、'.join(industries[:3])} 找到 {len(industry_evidence)} 条近期景气、政策或风险资料，详见证据来源。"
                    )
            except Exception as exc:
                unknowns.append("行业补充检索失败：" + _external_error(exc))
        if focus.startswith("统筹追问：") and self.tavily_client is not None:
            scoped = [
                symbol
                for symbol in (focus_symbols or preferred_symbols)
                if symbol in {row.symbol for row in report.stocks}
            ][:3]
            for symbol in scoped:
                try:
                    response = self.tavily_client.search(
                        f"A股 {symbol} {focus.removeprefix('统筹追问：')[:500]}",
                        max_results=6,
                        time_range="year",
                    )
                    follow_up_evidence = _tavily_evidence(
                        response,
                        symbols=[symbol],
                        assign_unmatched=True,
                    )
                    evidence.extend(follow_up_evidence)
                    lines.append(
                        f"统筹追问补查：{symbol} 围绕“{focus.removeprefix('统筹追问：')[:160]}”"
                        f"取得 {len(follow_up_evidence)} 条联网资料。"
                    )
                except Exception as exc:
                    unknowns.append(f"{symbol} 统筹追问补查失败：" + _external_error(exc))
        return AgentContribution(
            agent_id="company_industry",
            summary="\n\n".join(lines) if lines else "本轮没有取得可核验的公司与行业资料。",
            claims=claims,
            evidence=evidence,
            unknowns=unknowns,
        )

    def _global_market_contribution(self, snapshot: dict) -> AgentContribution:
        indices = list(snapshot.get("market_indices") or [])
        status = str(snapshot.get("status") or "unavailable")
        evidence: list[EvidenceItem] = []
        claims: list[Claim] = []
        lines = [str(snapshot.get("notice") or "外围指数数据不可用。")]
        for region in ("美国", "韩国", "日本"):
            regional = [item for item in indices if item.get("region") == region]
            if not regional:
                continue
            moves = []
            for item in regional:
                change_percent = float(item.get("change_percent") or 0)
                direction = "上涨" if change_percent > 0 else "下跌" if change_percent < 0 else "持平"
                moves.append(
                    f"{item['name']} {direction} {abs(change_percent):.2f}%"
                    f"（{item.get('trade_date') or '日期未知'}，{item.get('timezone') or '时区未知'}）"
                )
                if status == "live_delayed":
                    evidence_item = EvidenceItem(
                        source_type="market_data",
                        title=f"{item['name']} 延迟指数行情",
                        excerpt=(
                            f"最近收盘 {item.get('close')}，前收 {item.get('previous_close')}，"
                            f"涨跌幅 {change_percent:+.2f}%。"
                        ),
                        url=str(item.get("source_url") or ""),
                        published_at=str(item.get("trade_date") or ""),
                        symbols=[str(item.get("ticker") or "")],
                    )
                    evidence.append(evidence_item)
                    claims.append(
                        Claim(
                            text=moves[-1],
                            kind="fact",
                            symbols=[str(item.get("ticker") or "")],
                            evidence_ids=[evidence_item.evidence_id],
                            confidence="medium",
                        )
                    )
            lines.append(region + "市场：" + "；".join(moves) + "。")
        if indices:
            positive = sum(1 for item in indices if float(item.get("change_percent") or 0) > 0)
            negative = sum(1 for item in indices if float(item.get("change_percent") or 0) < 0)
            lines.append(f"五个核心指数中 {positive} 个上涨、{negative} 个下跌；这里只描述背景，不推导 A 股必然方向。")
        quality_flags = [str(item) for item in snapshot.get("quality_flags") or []]
        if quality_flags:
            lines.append("数据质量提示：" + "；".join(quality_flags) + "。")
        unknowns = []
        if status == "demo_fallback":
            unknowns.append("外围行情接口不可用，当前图表为明确标注的演示占位数据")
            claims.append(Claim(text="当前外围市场卡片为演示数据。", kind="limitation", confidence="high"))
        unknowns.extend(str(item) for item in snapshot.get("errors") or [])
        unknowns.extend(quality_flags)
        return AgentContribution(
            agent_id="global_market",
            summary="\n".join(lines),
            claims=claims,
            evidence=evidence,
            unknowns=unknowns,
            structured_data=snapshot,
        )

    def _review(
        self,
        run_id: str,
        report: ParsedReport,
        contributions: list[AgentContribution],
        task: AgentTask,
    ) -> AgentContribution:
        scope = self._workflow_step(
            task,
            "risk_scope",
            lambda: _risk_search_scope(report, contributions, limit=5),
        )
        search_result = self._workflow_step(
            task,
            "negative_news_search",
            lambda: self._search_negative_news(report, scope, task.instructions),
        )
        flagged = self._workflow_step(
            task,
            "evidence_filter",
            lambda: _filter_negative_news(
                search_result["evidence"], report_date=report.report_date
            ),
        )
        fallback = _negative_news_fallback(
            scope,
            flagged,
            report_date=report.report_date,
            unknowns=search_result["unknowns"],
        )
        return self._workflow_step(
            task,
            "risk_summary",
            lambda: self._llm_review(run_id, report, contributions, fallback),
        )

    def _search_negative_news(
        self,
        report: ParsedReport,
        scope: list[dict],
        focus: str = "",
    ) -> dict:
        evidence: list[EvidenceItem] = []
        unknowns: list[str] = []
        symbols = [str(item["symbol"]) for item in scope]

        if self.public_a_stock_client is not None and symbols:
            try:
                bundles = self.public_a_stock_client.research(symbols, max_stocks=len(symbols))
                evidence.extend(_risk_public_evidence(bundles))
            except Exception as exc:
                unknowns.append("公告与公开新闻检索失败：" + _external_error(exc))
        else:
            unknowns.append("本轮未启用公告与公开新闻检索工具")

        if self.tavily_client is not None and scope:
            def search_one(item: dict) -> tuple[str, list[EvidenceItem], str]:
                symbol = str(item["symbol"])
                name = str(item.get("name") or symbol)
                code = symbol.split(".", 1)[0]
                query = (
                    f"A股 {name} {code} 利空 负面 公告 减持 立案 调查 处罚 问询 诉讼 "
                    f"预亏 下修 质押 冻结 解禁 违约 退市 风险 截至 {report.report_date or '今天'}"
                )
                if focus.startswith("统筹追问："):
                    query += " " + focus.removeprefix("统筹追问：")[:500]
                try:
                    response = self.tavily_client.search(query, max_results=6, time_range="year")
                    found = _tavily_evidence(response, symbols=[symbol], assign_unmatched=True)
                    return symbol, found, ""
                except Exception as exc:
                    return symbol, [], _external_error(exc)

            with ThreadPoolExecutor(max_workers=min(3, len(scope))) as executor:
                futures = [executor.submit(search_one, item) for item in scope]
                for future in as_completed(futures):
                    symbol, found, error = future.result()
                    evidence.extend(found)
                    if error:
                        unknowns.append(f"{symbol} 补充联网检索失败：{error}")
        else:
            unknowns.append("Tavily 未配置，本轮只使用公告与公开新闻来源")

        return {"evidence": evidence, "unknowns": list(dict.fromkeys(unknowns))}

    def _llm_review(
        self,
        run_id: str,
        report: ParsedReport,
        _contributions: list[AgentContribution],
        fallback: AgentContribution,
    ) -> AgentContribution:
        if self.llm_client is None:
            return fallback
        all_evidence = list(fallback.evidence)
        user = json.dumps(
            {
                "report_context": {
                    "report_date": report.report_date,
                    "symbols": (fallback.structured_data.get("risk_search") or {}).get("symbols", []),
                },
                "deterministic_fallback": fallback.model_dump(mode="json", exclude={"evidence"}),
                "evidence_registry": [item.model_dump(mode="json") for item in all_evidence],
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            result = self.llm_client.complete_json(
                self._prompt(AGENT_PROMPT_IDS["risk"], RISK_PROMPT)[0], user
            )
            candidate = AgentContribution.model_validate(result.data)
            if candidate.agent_id != "risk":
                raise ValueError("风险 Agent 返回了错误的 agent_id")
            valid_ids = {item.evidence_id for item in all_evidence}
            for claim in candidate.claims:
                claim.evidence_ids = [item for item in claim.evidence_ids if item in valid_ids]
            known_claims = {claim.text for claim in candidate.claims}
            candidate.claims.extend(claim for claim in fallback.claims if claim.text not in known_claims)
            candidate.risks = list(dict.fromkeys([*candidate.risks, *fallback.risks]))
            candidate.unknowns = list(dict.fromkeys([*candidate.unknowns, *fallback.unknowns]))
            candidate.evidence = fallback.evidence
            candidate.structured_data = fallback.structured_data
            self._emit(
                run_id,
                "model.usage",
                agent_id="risk",
                payload={
                    "stage": "risk_review",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return candidate
        except Exception as exc:
            self._emit(
                run_id,
                "model.fallback",
                agent_id="risk",
                payload={"stage": "risk_review", "error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback

    def _synthesize(
        self,
        run_id: str,
        report: ParsedReport,
        contributions: list[AgentContribution],
        review: AgentContribution,
        steering: list[str],
        *,
        state: ResearchState | None = None,
    ) -> dict[str, object]:
        recommendations = _recommendation_cards(report, contributions, review)
        recommendation_labels = [str(item["label"]) for item in recommendations]
        if recommendations:
            executive_summary = (
                f"按既定量化规则，本轮推荐关注 {len(recommendations)} 只："
                + "、".join(recommendation_labels)
                + "。以下只保留入选量化依据、消息面和具体风险。"
            )
        else:
            executive_summary = "按既定量化规则，本轮没有股票满足正式推荐条件。"
        evidence_gaps = _material_evidence_gaps(report, contributions, review)
        fallback: dict[str, object] = {
            "title": "统筹规则推荐",
            "report_id": report.report_id,
            "parse_status": report.parse_status,
            "executive_summary": executive_summary,
            "recommendations": recommendations,
            "signal_interpretation": [
                f"{item['label']}：{item['quant_summary']}" for item in recommendations
            ],
            "news_summary": [
                f"{item['label']}：{item['news_summary']}" for item in recommendations
            ],
            "risk_notes": [
                f"{item['label']}：{item['risk_summary']}" for item in recommendations
            ],
            "evidence_gaps": evidence_gaps,
            "contributions": [item.model_dump(mode="json") for item in contributions],
            "risk_review": review.model_dump(mode="json"),
            "steering_applied": steering,
            "disclaimer": "规则筛选结果仅供研究，不构成交易指令。",
        }
        if state is not None:
            fallback["research_state"] = state.model_dump(mode="json")
            fallback["cross_domain"] = _deterministic_cross_domain(recommendations, contributions)
        if self.llm_client is None:
            return fallback
        system = self._prompt("coordinator.synthesis", BRAIN_SYNTHESIS_PROMPT)[0]
        user_payload: dict[str, object] = {
            "report_id": report.report_id,
            "parse_status": report.parse_status,
            "rule_recommendations": recommendations,
            "compact_evidence": [
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "news_summary": item["news_summary"],
                    "risk_summary": item["risk_summary"],
                }
                for item in recommendations
            ],
            "risk_review_unknowns": evidence_gaps,
            "steering": steering,
        }
        if state is not None:
            user_payload["research_state"] = state.model_dump(mode="json")
            user_payload["event_findings"] = fallback.get("cross_domain") or []
        user = json.dumps(user_payload, ensure_ascii=False, default=str)
        try:
            result = self.llm_client.complete_json(system, user)
            data = result.data
            required = {"news_summary", "risk_notes", "evidence_gaps"}
            if not required.issubset(data):
                raise ValueError("大脑模型输出缺少必需字段")
            symbols = [str(item["symbol"]) for item in recommendations]
            raw_cross_domain = data.get("cross_domain") or []
            # 合并规则：专项 Agent 的确定性结论（资金行为分类/利空长短分类/
            # 板块共振背离）是程序权威结果，模型不得用"未取得"等占位文字覆盖；
            # overall_view 等判断性字段以模型为准，模型缺失时用确定性兜底。
            deterministic_cross = {
                str(item.get("symbol")): dict(item)
                for item in fallback.get("cross_domain") or []
            }
            for item in raw_cross_domain:
                if not isinstance(item, dict) or str(item.get("symbol") or "") not in symbols:
                    continue
                symbol = str(item["symbol"])
                det = deterministic_cross.get(symbol, {"symbol": symbol})
                merged = dict(item)
                merged["symbol"] = symbol
                for key in ("capital_behavior", "bearish_outlook", "sector_transmission"):
                    det_value = str(det.get(key) or "").strip()
                    if det_value and det_value not in (
                        "本轮未取得相关结论",
                        "本轮外围市场无异常板块",
                    ):
                        merged[key] = det_value
                    elif not str(merged.get(key) or "").strip():
                        merged[key] = det_value or "本轮未取得相关结论"
                if not str(merged.get("overall_view") or "").strip():
                    merged["overall_view"] = str(det.get("overall_view") or "")
                deterministic_cross[symbol] = merged
            cross_domain = [deterministic_cross[symbol] for symbol in symbols if symbol in deterministic_cross]
            final = dict(fallback)
            final.update(
                {
                    "news_summary": _merge_symbol_summaries(
                        data.get("news_summary"),
                        fallback["news_summary"],
                        symbols,
                    ),
                    "risk_notes": _merge_symbol_summaries(
                        data.get("risk_notes"),
                        fallback["risk_notes"],
                        symbols,
                    ),
                    "evidence_gaps": _compact_model_list(
                        data.get("evidence_gaps"), evidence_gaps, limit=3
                    ),
                    "cross_domain": cross_domain,
                    "model": result.model,
                }
            )
            self._emit(
                run_id,
                "model.usage",
                agent_id="brain",
                payload={
                    "stage": "synthesis",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return final
        except Exception as exc:
            self._emit(
                run_id,
                "model.fallback",
                agent_id="brain",
                payload={"stage": "synthesis", "error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback

    def _llm_contribution(
        self,
        task: AgentTask,
        report: ParsedReport,
        fallback: AgentContribution,
    ) -> AgentContribution:
        if self.llm_client is None:
            return fallback
        fallback_prompt = AGENT_PROMPTS.get(
            task.agent_id, "只能依据输入中的报告和证据完成职责，不得创造事实。"
        )
        prompt_id = AGENT_PROMPT_IDS.get(task.agent_id, f"{task.agent_id}.system")
        system = self._prompt(prompt_id, fallback_prompt)[0]
        compact_report = _compact_report(report, include_unknown=task.agent_id == "quant_signal")
        model_input = {
            "task": task.model_dump(mode="json"),
            "report": compact_report,
            "deterministic_fallback": fallback.model_dump(mode="json", exclude={"evidence"}),
            "minimum_evidence": [item.model_dump(mode="json") for item in fallback.evidence],
        }
        if task.agent_id == "quant_signal":
            model_input["strategy_inputs"] = compact_report.pop("stocks", [])
        user = json.dumps(model_input, ensure_ascii=False, default=str)
        try:
            result = self.llm_client.complete_json(system, user)
            # 模型常把输入里的元数据原样抄回输出（task_id/run_id/report_id/
            # generated_at 等），StrictModel 的 extra=forbid 会把这种输出
            # 整个判废。这些键只是回显、不携带信息，校验前剥掉，其余
            # 未知键仍然按严格模式拒绝，安全边界不变。
            data = _strip_echo_fields(result.data)
            # summary 是必填字段，而模型截断时最先丢的往往就是它。
            # 补上确定性兜底的 summary，claim/evidence 校验仍然全量执行——
            # 模型说不出话时，至少确定性内容完整可用，而不是整份作废。
            if not str(data.get("summary") or "").strip():
                data["summary"] = fallback.summary
            candidate = AgentContribution.model_validate(data)
            if candidate.agent_id != task.agent_id:
                raise ValueError("专业 Agent 返回了错误的 agent_id")
            valid_ids = {item.evidence_id for item in fallback.evidence}
            for claim in candidate.claims:
                claim.evidence_ids = [item for item in claim.evidence_ids if item in valid_ids]
            if task.agent_id == "quant_signal" and not _safe_quant_rewrite(candidate.summary, fallback.summary):
                raise ValueError("量化 Agent 改动或遗漏了确定性结果")
            candidate.evidence = fallback.evidence
            candidate.structured_data = fallback.structured_data
            candidate.risks = list(dict.fromkeys([*candidate.risks, *fallback.risks]))
            candidate.unknowns = list(dict.fromkeys([*candidate.unknowns, *fallback.unknowns]))
            self._emit(
                task.run_id,
                "model.usage",
                agent_id=task.agent_id,
                payload={
                    "stage": "specialist",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return candidate
        except Exception as exc:
            self._emit(
                task.run_id,
                "model.fallback",
                agent_id=task.agent_id,
                payload={"stage": "specialist", "error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback

    def _task_title(self, agent_id: str) -> str:
        return {
            "quant_signal": "检查量化信号与数据质量",
            "company_industry": "核验公司与行业背景",
            "global_market": "汇总美股、韩国与日本核心指数走势",
            "risk": "逐票检索近期负面公告与新闻",
            "capital_trace": "追查资金异常标的的资金流历史",
            "bearish_analysis": "分析利空事件的持续性与影响范围",
            "global_sector_flow": "定位外围异常板块",
            "sector_transmission": "映射外围板块到 A 股板块",
        }.get(agent_id, "执行专业分析")

    def _prompt(self, prompt_id: str, fallback: str) -> tuple[str, str]:
        platform, platform_version = self.repository.published_prompt(
            "platform.policy", PLATFORM_POLICY_PROMPT
        )
        role, role_version = self.repository.published_prompt(prompt_id, fallback)
        return (
            platform + "\n\n--- 专业角色系统指令 ---\n" + role,
            f"platform.policy:v{platform_version}+{prompt_id}:v{role_version}",
        )

    def _emit(self, run_id: str, kind: str, *, agent_id: str = "", payload: dict | None = None) -> None:
        with self._lock:
            self._seq[run_id] = self._seq.get(run_id, 0) + 1
            event = HarnessEvent(
                seq=self._seq[run_id],
                run_id=run_id,
                kind=kind,
                agent_id=agent_id,
                payload=payload or {},
            )
            self.repository.append_event(event)
        self.event_sink(event)


def _agent_display_name(agent_id: str) -> str:
    profile = _PROFILE_BY_ID.get(agent_id)
    return profile.display_name if profile else agent_id


def _agent_responsibility(agent_id: str) -> str:
    specific = {
        "quant_signal": "复核 PTrade 量化字段、正式观察和候选规则，只解释确定性数据",
        "company_industry": "查询公司身份、财务、公告、新闻和行业资料，可按大脑补充需求补查",
        "global_market": "核对报告日对应的美股、韩国与日本指数日期、点位和涨跌",
        "risk": "逐票检索报告日前的负面公告和新闻，并总结有来源的潜在利空",
        "brain": "确定核心问题、审阅研究状态并做跨域定性；不碰工具、不派单",
        "capital_trace": "查询资金异常标的近 10 日资金流历史，区分单日脉冲与持续异动",
        "bearish_analysis": "按规则区分结构性（长期）与事件性（短期）利空并说明影响范围",
        "global_sector_flow": "查询外围行业 ETF 涨跌并定位异常板块",
        "sector_transmission": "把外围异常板块映射到 A 股对应板块并判定共振或背离",
    }
    if agent_id in specific:
        return specific[agent_id]
    profile = _PROFILE_BY_ID.get(agent_id)
    return profile.description if profile else "执行现有职责范围内的补充分析"


def _compact_contribution_for_review(contribution: AgentContribution | None) -> dict | None:
    if contribution is None:
        return None
    return {
        "agent_id": contribution.agent_id,
        "summary": contribution.summary[:1800],
        "claims": [claim.model_dump(mode="json") for claim in contribution.claims[:12]],
        "risks": contribution.risks[:10],
        "unknowns": contribution.unknowns[:10],
        "follow_up_requests": contribution.follow_up_requests[:8],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "source_type": item.source_type,
                "title": item.title,
                "excerpt": item.excerpt[:220],
                "published_at": item.published_at,
                "symbols": item.symbols,
            }
            for item in contribution.evidence[:12]
        ],
    }


def _latest_contributions_by_agent(
    contributions: list[AgentContribution],
) -> list[AgentContribution]:
    latest: dict[str, AgentContribution] = {}
    for contribution in contributions:
        latest[contribution.agent_id] = contribution
    return list(latest.values())


def _follow_up_signature(agent_id: str, instructions: str, symbols: list[str]) -> str:
    normalized = re.sub(r"\s+", " ", instructions).strip().lower()
    return f"{agent_id}|{','.join(sorted(symbols))}|{normalized}"


def _validated_agent_calls(
    raw: object,
    available: list[str],
    valid_symbols: set[str],
    *,
    limit: int = 3,
) -> list[dict]:
    """校验大脑的研究指令：agent 白名单、symbols 交集、问题长度、去重。"""
    calls: list[dict] = []
    seen: set[str] = set()
    for item in list(raw or [])[:limit]:
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id") or "")
        question = str(item.get("question") or "").strip()[:1000]
        if agent_id not in available or len(question) < 8:
            continue
        symbols = [str(symbol) for symbol in item.get("symbols") or [] if str(symbol) in valid_symbols][:3]
        reason = str(item.get("reason") or "").strip()[:500]
        priority = int(item.get("priority") or 2)
        normalized_question = re.sub(r"\s+", " ", question).strip().lower()
        signature = f"{agent_id}|{normalized_question}"
        if signature in seen:
            continue
        seen.add(signature)
        calls.append(
            {
                "agent_id": agent_id,
                "question": question,
                "symbols": symbols,
                "reason": reason,
                "priority": priority,
            }
        )
    return calls


def _compact_structured(structured: dict) -> dict:
    """压缩 structured_data 进 ResearchState：列表/字典截断，标量保留。"""
    compact: dict = {}
    for key, value in structured.items():
        if isinstance(value, list):
            compact[key] = value[:8]
        elif isinstance(value, dict):
            compact[key] = dict(list(value.items())[:8])
        else:
            compact[key] = value
    return compact


def _deterministic_cross_domain(
    recommendations: list[dict], contributions: list[AgentContribution]
) -> list[dict]:
    """从事件 Agent 的确定性结论构建逐票跨域判断；模型综合时在此基础上覆盖。"""
    latest = {item.agent_id: item for item in _latest_contributions_by_agent(contributions)}
    # 同一 Agent 可能被派发多次（规则触发 + 大脑补查），per_symbol 按标的聚合，
    # 保证每一票的结论都进跨域判断；transmission 为市场级结论，取最新一份。
    capital_per: dict[str, dict] = {}
    for contribution in contributions:
        if contribution.agent_id != "capital_trace":
            continue
        for item in (contribution.structured_data or {}).get("per_symbol") or []:
            capital_per.setdefault(str(item.get("symbol")), item)
    bearish_per: dict[str, dict] = {}
    for contribution in contributions:
        if contribution.agent_id != "bearish_analysis":
            continue
        for symbol, item in ((contribution.structured_data or {}).get("per_symbol") or {}).items():
            bearish_per.setdefault(str(symbol), item)
    transmission = latest.get("sector_transmission")
    transmissions: list[dict] = []
    if transmission is not None:
        transmissions = (transmission.structured_data or {}).get("transmissions") or []
    transmission_text = "；".join(
        f"{item.get('foreign_name')} {float(item.get('foreign_change_percent') or 0):+.2f}%"
        f"→A股映射板块："
        + "、".join(board.get("name", "") for board in item.get("a_share_boards") or [])
        for item in transmissions
    )
    cross_domain: list[dict] = []
    for item in recommendations:
        symbol = str(item["symbol"])
        entry: dict = {"symbol": symbol}
        capital_item = capital_per.get(symbol)
        if capital_item and capital_item.get("classification"):
            entry["capital_behavior"] = _CAPITAL_CLASSIFICATION_LABELS.get(
                str(capital_item["classification"]), "本轮未取得相关结论"
            )
        bearish_item = bearish_per.get(symbol) or {}
        structural = [x.get("title") for x in bearish_item.get("structural") or []]
        event_driven = [x.get("title") for x in bearish_item.get("event_driven") or []]
        if structural or event_driven:
            parts = []
            if structural:
                parts.append("结构性（长期）：" + "；".join(structural[:2]))
            if event_driven:
                parts.append("事件性（短期）：" + "；".join(event_driven[:2]))
            entry["bearish_outlook"] = "。".join(parts)
        entry.setdefault("capital_behavior", "本轮未取得相关结论")
        entry.setdefault("bearish_outlook", "本轮未取得相关结论")
        entry.setdefault(
            "sector_transmission",
            transmission_text[:400] if transmission_text else "本轮外围市场无异常板块",
        )
        entry["overall_view"] = "见规则推荐卡片的量化依据与消息面、风险摘要。"
        cross_domain.append(entry)
    return cross_domain


def _event_inputs(task: AgentTask) -> dict:
    """从事件任务书中取回触发器数据（_event_task 嵌入的 JSON）。"""
    match = re.search(r"触发器数据：(.*?)。职责内完成分析", task.instructions, flags=re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _event_scope_for(record: TriggerRecord, report: ParsedReport) -> set[str]:
    """一次事件派单覆盖的标的范围（用于大脑点名时的覆盖度去重）。

    市场级 Agent（外围板块资金/板块传导映射）覆盖全市场，记为 {"*"}；
    利空分析覆盖全部报告标的；资金追查只覆盖触发器命中的标的。
    """
    if record.agent_id in ("global_sector_flow", "sector_transmission"):
        return {"*"}
    if record.agent_id == "bearish_analysis":
        return {row.symbol for row in report.stocks}
    rows = (record.inputs or {}).get("rows") or []
    return {str(row.get("symbol")) for row in rows if row.get("symbol")}


def _capital_trigger_rows(
    report: ParsedReport,
    inputs: dict,
    restrict_symbols: list[str] | None = None,
) -> list[dict]:
    """锁定资金异常标的：优先用触发器数据，缺失时按同一口径从报告重算。

    大脑点名补查时用 restrict_symbols 把范围收窄到大脑指定的标的。
    """
    rows: list[dict] = []
    raw_rows = list(inputs.get("rows") or [])
    if not raw_rows:
        for row in report.stocks:
            threshold = _row_effective_threshold(row)
            formula = row.realtime_formula_wanyuan
            if row.super_large_anomaly is True or (
                formula is not None and threshold is not None and formula >= threshold * 2
            ):
                raw_rows.append(
                    {
                        "symbol": row.symbol,
                        "super_large_anomaly": row.super_large_anomaly,
                        "realtime_formula_wanyuan": float(formula) if formula is not None else None,
                        "effective_threshold": float(threshold) if threshold is not None else None,
                    }
                )
    for item in raw_rows:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        if restrict_symbols is not None and str(item["symbol"]) not in restrict_symbols:
            continue
        stock = next((row for row in report.stocks if row.symbol == item["symbol"]), None)
        rows.append(
            {
                "symbol": str(item["symbol"]),
                "name": stock.name if stock else "",
                "super_large_anomaly": bool(item.get("super_large_anomaly")),
                "realtime_formula_wanyuan": item.get("realtime_formula_wanyuan"),
                "effective_threshold": item.get("effective_threshold"),
            }
        )
    if restrict_symbols is not None:
        # 大脑点名补查：范围以大脑指定的标的为准（规则未命中也可以查）。
        for symbol in restrict_symbols:
            if any(row["symbol"] == symbol for row in rows):
                continue
            stock = next((row for row in report.stocks if row.symbol == symbol), None)
            if stock is None:
                continue
            threshold = _row_effective_threshold(stock)
            rows.append(
                {
                    "symbol": symbol,
                    "name": stock.name,
                    "super_large_anomaly": stock.super_large_anomaly,
                    "realtime_formula_wanyuan": (
                        float(stock.realtime_formula_wanyuan)
                        if stock.realtime_formula_wanyuan is not None
                        else None
                    ),
                    "effective_threshold": (
                        float(threshold) if threshold is not None else None
                    ),
                }
            )
    rows.sort(key=lambda item: abs(item["realtime_formula_wanyuan"] or 0), reverse=True)
    return rows[:3]


def _classify_capital_pulses(history: dict) -> dict:
    """确定性分类：单日脉冲 / 持续流入 / 持续流出 / 数据不足。"""
    classified: dict = {}
    for symbol, item in history.items():
        rows = [row for row in item.get("rows") or [] if row.get("net") is not None]
        base = {**item, "rows": rows[-10:]}
        if len(rows) < 3:
            classified[symbol] = {
                **base,
                "classification": "insufficient_data",
                "today_net": None,
                "median_5d": None,
            }
            continue
        today_net = float(rows[-1]["net"])
        prior = rows[-6:-1] if len(rows) >= 6 else rows[:-1]
        median_5d = statistics.median(abs(float(row["net"])) for row in prior)
        recent = rows[-5:]
        if len(prior) >= 5 and median_5d > 0 and abs(today_net) >= 2 * median_5d:
            classification = "single_day_pulse"
        elif sum(1 for row in recent if float(row["net"]) > 0) >= 3:
            classification = "persistent_inflow"
        elif sum(1 for row in recent if float(row["net"]) < 0) >= 3:
            classification = "persistent_outflow"
        else:
            classification = "single_day_pulse"
        classified[symbol] = {
            **base,
            "classification": classification,
            "today_net": round(today_net, 2),
            "median_5d": round(median_5d, 2),
        }
    return classified


_BEARISH_STRUCTURAL_WORDS = (
    "立案", "调查", "退市", "风险警示", "ST", "诉讼", "仲裁", "违约", "逾期",
    "资金占用", "担保", "破产", "重整", "失信",
)
_BEARISH_EVENT_WORDS = (
    "减持", "解禁", "质押", "问询", "警示函", "预亏", "下修", "亏损", "停产",
    "事故", "召回", "减值",
)


def _bearish_duration(text: str) -> str:
    if _contains_risk_keyword(text, _BEARISH_STRUCTURAL_WORDS):
        return "structural"
    if _contains_risk_keyword(text, _BEARISH_EVENT_WORDS):
        return "event_driven"
    return "unclassified"


def _classify_bearish_duration(searched: dict) -> dict:
    """对补查证据做长期/短期 rubric 分类，保留证据与失败项。"""
    evidence = list(searched.get("evidence") or [])
    per_item = {
        item.evidence_id: _bearish_duration(str(item.title) + " " + str(item.excerpt))
        for item in evidence
    }
    return {
        "evidence": evidence,
        "unknowns": list(searched.get("unknowns") or []),
        "per_item": per_item,
    }


def _anomaly_sectors(snapshot: dict) -> list[dict]:
    sectors = sorted(
        (item for item in snapshot.get("sectors") or [] if item.get("change_percent") is not None),
        key=lambda item: abs(float(item["change_percent"])),
        reverse=True,
    )
    top: list[dict] = []
    for item in sectors[:3]:
        change = float(item["change_percent"])
        if abs(change) >= EVENT_SECTOR_MOVE_THRESHOLD_PCT:
            top.append(
                {
                    "ticker": str(item["ticker"]),
                    "name": str(item["name"]),
                    "change_percent": round(change, 2),
                    "direction": "上涨" if change > 0 else "下跌",
                    "trade_date": str(item.get("trade_date") or ""),
                }
            )
    return top


def _resolve_transmissions(anomaly: list[dict]) -> list[dict]:
    resolved: list[dict] = []
    for item in anomaly:
        ticker = str(item.get("ticker") or "")
        entry = SECTOR_TRANSMISSION_MAP.get(ticker)
        if not entry:
            continue
        resolved.append(
            {
                "foreign_ticker": ticker,
                "foreign_name": str(item.get("name") or entry["name"]),
                "foreign_change_percent": float(item.get("change_percent") or 0),
                "a_share_keywords": list(entry["a_share_boards"]),
            }
        )
    return resolved


def _rank_transmissions(resolved: list[dict], payload: dict) -> list[dict]:
    boards = list(payload.get("boards") or [])
    transmissions: list[dict] = []
    for item in resolved:
        keywords = item["a_share_keywords"]
        matched = [
            board
            for board in boards
            if any(keyword in str(board.get("name") or "") for keyword in keywords)
        ]
        matched.sort(key=lambda board: abs(float(board.get("change_percent") or 0)), reverse=True)
        foreign_change = item["foreign_change_percent"]
        rows = []
        for board in matched[:3]:
            board_change = float(board.get("change_percent") or 0)
            if foreign_change > 0 and board_change > 0 or foreign_change < 0 and board_change < 0:
                direction = "共振"
            elif foreign_change * board_change < 0:
                direction = "背离"
            else:
                direction = "持平"
            rows.append(
                {
                    "code": str(board.get("code") or ""),
                    "name": str(board.get("name") or ""),
                    "change_percent": round(board_change, 2),
                    "direction": direction,
                }
            )
        transmissions.append(
            {
                "foreign_ticker": item["foreign_ticker"],
                "foreign_name": item["foreign_name"],
                "foreign_change_percent": round(foreign_change, 2),
                "a_share_boards": rows,
            }
        )
    transmissions.sort(key=lambda item: abs(item["foreign_change_percent"]), reverse=True)
    return transmissions[:5]


# 模型回显输入元数据时剥掉的键。它们不含专业判断，只是把输入里的
# task/report 标识原样抄回了输出；StrictModel 的 extra=forbid 会把整份
# 输出判废，白白烧一次调用。剥掉后其余未知键仍然严格拒绝。
_ECHO_FIELDS = {
    "task_id",
    "run_id",
    "report_id",
    "generated_at",
    "workflow_id",
    "workflow_version",
    "run_slot",
    "parse_status",
    "diagnostics",
    "stocks",
    "deterministic_fallback",
    "minimum_evidence",
    "strategy_inputs",
    "report",
    "task",
}


def _strip_echo_fields(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return data
    # 模型偶尔把整个输出包进 {"contribution": {...}} 壳里；AgentContribution
    # 没有同名字段，取内层不会误伤合法输出。
    inner = data.get("contribution")
    if isinstance(inner, dict):
        data = inner
    return {key: value for key, value in data.items() if key not in _ECHO_FIELDS}


def _compact_report(report: ParsedReport, *, include_unknown: bool = False) -> dict[str, object]:
    excluded_fields = {"raw_row"} if include_unknown else {"raw_row", "unknown_fields"}
    return {
        "report_id": report.report_id,
        "generated_at": report.generated_at,
        "run_slot": report.run_slot,
        "parse_status": report.parse_status,
        "diagnostics": report.diagnostics,
        "stocks": [row.model_dump(mode="json", exclude=excluded_fields) for row in report.stocks],
    }


def _row_effective_threshold(row: ReportStock) -> Decimal:
    """该标的的真实资金门槛（单位万元）。

    1000 亿以上通道的门槛是 0.4%×市值，逐票不同；其余沿用解析器
    注入的 4000 万参考口径。反推市值落在 1000 亿边界模糊窗口内时
    回退参考口径（见 parser.tiered_flow_threshold_wanyuan）。
    """
    tiered = tiered_flow_threshold_wanyuan(
        row.realtime_formula_wanyuan, row.realtime_formula_ratio_pct
    )
    if tiered is not None:
        return tiered
    return row.flow_threshold_wanyuan or Decimal("0")


def _number(value: Decimal | None) -> str:
    if value is None:
        return "缺失"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quant_metrics(row: ReportStock) -> str:
    formula = row.realtime_formula_wanyuan
    threshold = _row_effective_threshold(row) if row.realtime_formula_wanyuan is not None else None
    difference = formula - threshold if formula is not None and threshold is not None else None
    if difference is None:
        funding = "资金公式或门槛缺失"
    elif difference >= 0:
        funding = f"资金公式 {_number(formula)} 万元，比门槛高 {_number(difference)} 万元"
    else:
        funding = f"资金公式 {_number(formula)} 万元，比门槛低 {_number(abs(difference))} 万元"
    ratio = f"，占流通市值 {_number(row.realtime_formula_ratio_pct)}%" if row.realtime_formula_ratio_pct is not None else ""
    order_flow = (
        f"超大单 {_number(row.super_net_wanyuan)} 万元，"
        f"大单 {_number(row.large_net_wanyuan)} 万元，"
        f"中单 {_number(row.medium_net_wanyuan)} 万元，"
        f"小单 {_number(row.small_net_wanyuan)} 万元"
    )
    tape = (
        f"量比 {_number(row.vol_ratio)}，换手率 {_number(row.turnover_now_pct)}%，"
        f"外盘内盘条件{'通过' if row.l4_buy_sell is True else '未通过' if row.l4_buy_sell is False else '缺失'}"
    )
    structure = "，资金结构异常" if row.super_large_anomaly is True else ""
    return f"{funding}{ratio}；{order_flow}；{tape}{structure}。"


def _quant_stock_line(row: ReportStock, *, index: int, pool: str) -> str:
    return f"{index}. {_security_label(row)}（{pool}）：" + _quant_metrics(row)


def _flow_net(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _security_label(row: ReportStock) -> str:
    name = str(row.name or "").strip()
    return f"{row.symbol}｜{name}" if name else row.symbol


def _formal_recommendation_rows(report: ParsedReport) -> list[ReportStock]:
    rows = [row for row in report.selected_rows if row.reason == "all_conditions_met"]
    rows.sort(
        key=lambda row: (
            -(row.super_net_wanyuan or Decimal("0")),
            -(row.large_net_wanyuan or Decimal("0")),
            -(row.realtime_formula_ratio_pct or Decimal("0")),
            row.symbol,
        )
    )
    return rows


def _recommendation_cards(
    report: ParsedReport,
    contributions: list[AgentContribution],
    review: AgentContribution,
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for row in _formal_recommendation_rows(report):
        formula = row.realtime_formula_wanyuan or Decimal("0")
        threshold = _row_effective_threshold(row)
        surplus = formula - threshold
        quant_summary = (
            f"资金公式 {_number(formula)} 万元，门槛 {_number(threshold)} 万元，"
            f"高出 {_number(surplus)} 万元；超大单 {_number(row.super_net_wanyuan)} 万元；"
            f"量比 {_number(row.vol_ratio)}，换手率 {_number(row.turnover_now_pct)}%"
        )
        cards.append(
            {
                "symbol": row.symbol,
                "name": str(row.name or ""),
                "label": _security_label(row),
                "rule_status": "正式通过全部核心条件",
                "quant_summary": quant_summary,
                "news_summary": _news_summary_for_symbol(
                    contributions, row.symbol, str(row.name or "")
                ),
                "risk_summary": _risk_summary_for_symbol(review, row.symbol),
            }
        )
    return cards


def _news_summary_for_symbol(
    contributions: list[AgentContribution],
    symbol: str,
    name: str,
) -> str:
    priority = {"official_web": 0, "public_web": 1, "tavily": 2}
    candidates: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for contribution in contributions:
        if contribution.agent_id != "company_industry":
            continue
        for item in contribution.evidence:
            if symbol not in item.symbols or item.source_type not in priority:
                continue
            title = re.sub(r"\s+", " ", str(item.title or "")).strip()
            if not title or title in seen:
                continue
            if item.source_type != "official_web":
                code = symbol.partition(".")[0]
                if not any(value and value in title for value in (name, code, symbol)):
                    continue
            seen.add(title)
            candidates.append(
                (
                    priority[item.source_type],
                    str(item.published_at or "日期待核验"),
                    title[:120],
                )
            )
    candidates.sort(key=lambda value: value[0])
    if not candidates:
        return "本轮未取得明确消息面摘要"
    return "；".join(f"{date}《{title}》" for _priority, date, title in candidates[:2])


def _risk_summary_for_symbol(review: AgentContribution, symbol: str) -> str:
    lines = str(review.summary or "").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip().startswith(symbol)),
        None,
    )
    if start is not None:
        section: list[str] = []
        for line in lines[start + 1 :]:
            stripped = line.strip()
            if re.match(r"^\d{6}\.(?:SS|SZ|BJ)(?:｜|$)", stripped):
                break
            if stripped.startswith("-"):
                item = re.sub(
                    r"[（(]evidence_id:[^)）]+[)）]",
                    "",
                    stripped.lstrip("- "),
                )
                if any(
                    phrase in item
                    for phrase in ("未明确提及", "仅因检索命中", "是否涉及该公司", "无明确关联")
                ):
                    continue
                section.append(item.strip("； "))
            elif stripped and not section:
                section.append(stripped)
            if len(section) >= 2:
                break
        if section:
            return "；".join(item[:160] for item in section)
        return "截至报告日，本轮未检索到与该公司明确相关的利空消息；不代表不存在其他风险"
    evidence = [
        item
        for item in review.evidence
        if symbol in item.symbols and item.source_type in {"official_web", "public_web", "tavily"}
    ]
    if evidence:
        return "；".join(
            f"{item.published_at or '日期待核验'}《{str(item.title)[:120]}》"
            for item in evidence[:2]
        )
    return "截至报告日，本轮未检索到明确利空消息；不代表不存在其他风险"


def _material_evidence_gaps(
    report: ParsedReport,
    contributions: list[AgentContribution],
    review: AgentContribution,
) -> list[str]:
    gaps: list[str] = []
    if report.parse_status != "valid":
        gaps.append("报告解析不完整，部分量化字段缺失可能影响排序可靠性。")
    for value in [*review.unknowns, *(item for contribution in contributions for item in contribution.unknowns)]:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if any(token in text for token in ("IncompleteRead", "ReadTimeout", "ConnectTimeout")):
            match = re.search(r"\b\d{6}\.(?:SS|SZ|BJ)\b", text)
            text = (
                f"{match.group(0)} 联网补查未完成，消息面可能不完整。"
                if match
                else "部分联网补查未完成，消息面可能不完整。"
            )
        if text and text not in gaps:
            gaps.append(text[:220])
        if len(gaps) >= 3:
            break
    return gaps


def _compact_model_list(value: object, fallback: object, *, limit: int) -> list[str]:
    items = value if isinstance(value, list) else []
    cleaned = [re.sub(r"\s+", " ", str(item)).strip() for item in items]
    cleaned = [item for item in cleaned if item]
    if cleaned:
        return cleaned[:limit]
    return [str(item) for item in fallback][:limit] if isinstance(fallback, list) else []


def _merge_symbol_summaries(
    value: object,
    fallback: object,
    symbols: list[str],
) -> list[str]:
    fallback_items = [str(item) for item in fallback] if isinstance(fallback, list) else []
    candidate_items = _compact_model_list(value, [], limit=max(1, len(symbols) * 2))
    merged: list[str] = []
    for index, symbol in enumerate(symbols):
        candidate = next((item for item in candidate_items if symbol in item), "")
        if candidate:
            merged.append(candidate)
        elif index < len(fallback_items):
            merged.append(fallback_items[index])
    return merged


def _stability_note(value: dict) -> str:
    samples = int(value.get("sample_count") or 0)
    if samples <= 1:
        return " 稳定性记忆：目前只有本次样本，暂时不能判断历史稳定性。"
    formal = int(value.get("formal_count") or 0)
    candidates = int(value.get("candidate_count") or 0)
    missing = int(value.get("missing_runs") or 0)
    edge = value.get("average_funding_edge")
    edge_text = f"，平均资金余量 {edge} 万元" if edge is not None else ""
    return (
        f" 稳定性记忆：最近 {samples} 次中，正式观察 {formal} 次、候选 {candidates} 次、"
        f"字段缺失 {missing} 次{edge_text}。"
    )


def _named_values(values: dict, labels: dict[str, str]) -> str:
    return "，".join(
        f"{label} {values[key]}"
        for key, label in labels.items()
        if values.get(key) is not None and str(values.get(key)).strip() != ""
    )


def _company_stock_details(
    stock: ReportStock,
    snapshot: dict,
    public_bundle: dict,
    stable_profile: dict | None = None,
) -> dict:
    stable_profile = stable_profile or {}
    live_basic = snapshot.get("basic") or {}
    basic = live_basic
    company = snapshot.get("company") or {}
    daily = snapshot.get("daily_basic") or {}
    financial = snapshot.get("financial_indicator") or {}
    forecast = snapshot.get("forecast") or {}
    name = str(
        stable_profile.get("name")
        or basic.get("name")
        or company.get("com_name")
        or stock.name
        or stock.symbol
    )
    industry = str(stable_profile.get("industry") or basic.get("industry") or "")
    industry_label = industry or "行业未核验"
    evidence: list[EvidenceItem] = []
    evidence_ids: list[str] = []
    unknowns = [f"{stock.symbol} {error}" for error in snapshot.get("errors") or []]
    unknowns.extend(f"{stock.symbol} {error}" for error in public_bundle.get("errors") or [])

    identity = [f"名称 {name}", f"行业 {industry_label}"]
    if basic.get("market"):
        identity.append(f"市场 {basic['market']}")
    if basic.get("list_date"):
        identity.append(f"上市日期 {basic['list_date']}")
    if company.get("province") or company.get("city"):
        identity.append(f"地区 {company.get('province') or ''}{company.get('city') or ''}")
    if company.get("introduction"):
        identity.append("公司简介 " + str(company["introduction"])[:300])
    if live_basic or company:
        item = EvidenceItem(
            source_type="tushare",
            title=f"{stock.symbol} {name} · 公司与行业基础资料",
            excerpt="；".join(identity),
            url="https://tushare.pro/",
            symbols=[stock.symbol],
        )
        evidence.append(item)
        evidence_ids.append(item.evidence_id)
    stable_fields_used = bool(stable_profile.get("name") or stable_profile.get("industry"))
    if stable_fields_used:
        item = EvidenceItem(
            source_type="local_stable_master",
            title=f"{stock.symbol} {name} · 本地稳定证券映射",
            excerpt=f"名称 {name}；行业 {industry_label}",
            symbols=[stock.symbol],
        )
        evidence.append(item)
        evidence_ids.append(item.evidence_id)

    daily_text = _named_values(
        daily,
        {
            "trade_date": "日期",
            "close": "收盘价",
            "turnover_rate": "换手率%",
            "volume_ratio": "量比",
            "pe": "市盈率",
            "pb": "市净率",
            "total_mv": "总市值(万元)",
            "circ_mv": "流通市值(万元)",
        },
    )
    financial_text = _named_values(
        financial,
        {
            "end_date": "报告期",
            "eps": "每股收益",
            "roe": "净资产收益率%",
            "grossprofit_margin": "毛利率%",
            "netprofit_margin": "净利率%",
            "debt_to_assets": "资产负债率%",
            "current_ratio": "流动比率",
            "or_yoy": "营收同比%",
            "netprofit_yoy": "净利润同比%",
            "assets_yoy": "总资产同比%",
        },
    )
    if daily_text or financial_text:
        item = EvidenceItem(
            source_type="tushare",
            title=f"{stock.symbol} {name} · 估值与财务指标",
            excerpt="；".join(part for part in (daily_text, financial_text) if part),
            url="https://tushare.pro/",
            published_at=str(financial.get("ann_date") or daily.get("trade_date") or ""),
            symbols=[stock.symbol],
        )
        evidence.append(item)
        evidence_ids.append(item.evidence_id)

    forecast_text = _named_values(
        forecast,
        {
            "ann_date": "公告日期",
            "end_date": "报告期",
            "type": "业绩类型",
            "p_change_min": "净利润变动下限%",
            "p_change_max": "净利润变动上限%",
            "summary": "业绩摘要",
            "change_reason": "变动原因",
        },
    )
    if forecast_text:
        item = EvidenceItem(
            source_type="tushare",
            title=f"{stock.symbol} {name} · 业绩预告",
            excerpt=forecast_text,
            url="https://tushare.pro/",
            published_at=str(forecast.get("ann_date") or ""),
            symbols=[stock.symbol],
        )
        evidence.append(item)
        evidence_ids.append(item.evidence_id)

    public_counts = []
    for key, label, source_type in (
        ("announcements", "公告", "official_web"),
        ("news", "新闻", "public_web"),
        ("reports", "研报", "public_web"),
    ):
        sources = public_bundle.get(key) or []
        if sources:
            public_counts.append(f"{label} {len(sources)} 条")
        for source in sources:
            url = str(source.get("url") or "")
            if not url.startswith(("https://", "http://")):
                url = ""
            item = EvidenceItem(
                source_type=source_type,
                title=str(source.get("title") or f"{stock.symbol} {label}"),
                excerpt=str(source.get("summary") or source.get("type") or "")[:1200],
                url=url,
                published_at=str(source.get("date") or ""),
                symbols=[stock.symbol],
            )
            evidence.append(item)
            evidence_ids.append(item.evidence_id)

    sentences = [f"{stock.symbol}｜{name}｜{industry_label}。"]
    if daily_text:
        sentences.append("最近估值与交易指标：" + daily_text + "。")
    if financial_text:
        sentences.append("最近财务指标：" + financial_text + "。")
    if forecast_text:
        sentences.append("业绩预告：" + forecast_text + "。")
    if public_counts:
        sentences.append("公开资料找到" + "、".join(public_counts) + "，可在下方证据中查看原文。")
    if not evidence_ids:
        sentences.append("这只股票本轮没有取得可核验的公司资料。")
    return {
        "text": " ".join(sentences),
        "claim": f"{stock.symbol} 的公司与行业说明来自 {len(evidence_ids)} 条已登记资料。",
        "industry": industry,
        "evidence": evidence,
        "evidence_ids": evidence_ids,
        "unknowns": unknowns,
    }


def _risk_search_scope(
    report: ParsedReport,
    contributions: list[AgentContribution],
    *,
    limit: int,
) -> list[dict]:
    ordered = [*report.selected_rows, *report.near_rows]
    names: dict[str, str] = {row.symbol: row.name for row in ordered if row.name}
    for contribution in contributions:
        for item in contribution.evidence:
            if item.source_type not in {"tushare", "local_stable_master"}:
                continue
            for symbol in item.symbols:
                if names.get(symbol):
                    continue
                match = re.search(rf"{re.escape(symbol)}\s+(.+?)\s*·", item.title)
                if match:
                    names[symbol] = match.group(1).strip()
    scope = []
    seen: set[str] = set()
    for row in ordered:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        scope.append({"symbol": row.symbol, "name": names.get(row.symbol) or row.symbol})
        if len(scope) >= limit:
            break
    return scope


def _risk_public_evidence(bundles: list[dict]) -> list[EvidenceItem]:
    evidence: list[EvidenceItem] = []
    for bundle in bundles:
        symbol = str(bundle.get("symbol") or "")
        for key, source_type in (("announcements", "official_web"), ("news", "public_web")):
            for source in bundle.get(key) or []:
                url = str(source.get("url") or "").strip()
                if not url.startswith(("https://", "http://")):
                    url = ""
                evidence.append(
                    EvidenceItem(
                        source_type=source_type,
                        title=str(source.get("title") or f"{symbol} 公开资料").strip(),
                        excerpt=str(source.get("summary") or source.get("type") or "").strip()[:1200],
                        url=url,
                        published_at=str(source.get("date") or ""),
                        symbols=[symbol] if symbol else [],
                    )
                )
    return evidence


def _filter_negative_news(evidence: list[EvidenceItem], *, report_date: str) -> list[EvidenceItem]:
    filtered: list[EvidenceItem] = []
    seen: set[str] = set()
    per_symbol: Counter = Counter()
    cutoff = _normalized_date(report_date)
    for item in evidence:
        published = _normalized_date(item.published_at)
        if cutoff and published and published > cutoff:
            continue
        if not _contains_risk_keyword(f"{item.title} {item.excerpt}", RISK_KEYWORDS):
            continue
        symbol = item.symbols[0] if item.symbols else ""
        if not symbol or per_symbol[symbol] >= 5:
            continue
        key = (item.url.lower() if item.url else f"{symbol}|{item.published_at}|{item.title}").strip()
        if key in seen:
            continue
        seen.add(key)
        per_symbol[symbol] += 1
        filtered.append(item)
    return sorted(filtered, key=lambda item: (item.symbols[0] if item.symbols else "", item.published_at), reverse=True)


def _negative_news_fallback(
    scope: list[dict],
    evidence: list[EvidenceItem],
    *,
    report_date: str,
    unknowns: list[str],
) -> AgentContribution:
    date_label = report_date or "本次运行日"
    lines = [f"逐票利空检索（截至 {date_label}）："]
    risks: list[str] = []
    claims: list[Claim] = []
    hit_counts: dict[str, int] = {}
    for stock in scope:
        symbol = str(stock["symbol"])
        name = str(stock.get("name") or symbol)
        matches = [item for item in evidence if symbol in item.symbols]
        hit_counts[symbol] = len(matches)
        lines.append(f"\n{symbol}｜{name}")
        if not matches:
            lines.append("- 本轮公告和新闻来源未检索到明确利空消息；这不等于公司没有风险。")
            continue
        for item in matches:
            category = _risk_category(f"{item.title} {item.excerpt}")
            source = {
                "official_web": "官方公告",
                "public_web": "公开新闻",
                "tavily": "联网搜索",
            }.get(item.source_type, item.source_type)
            date = item.published_at or "日期待核验"
            detail = re.sub(r"\s+", " ", item.excerpt).strip()[:140]
            suffix = f"；{detail}" if detail else "；摘要不足，需打开原文核验"
            lines.append(f"- {date}｜{category}｜《{item.title}》（{source}）{suffix}")
            risk_text = f"{symbol}：{date}《{item.title}》"
            risks.append(risk_text)
            claims.append(
                Claim(
                    text=f"{symbol} 检索到可能的{category}消息：《{item.title}》。",
                    kind="risk",
                    symbols=[symbol],
                    evidence_ids=[item.evidence_id],
                    confidence="high" if item.source_type == "official_web" else "medium",
                )
            )
    if not scope:
        lines.append("报告中没有可供检索的股票代码。")
    if unknowns:
        lines.append("\n检索限制：" + "；".join(unknowns) + "。")
    return AgentContribution(
        agent_id="risk",
        summary="\n".join(lines),
        claims=claims,
        evidence=evidence,
        risks=list(dict.fromkeys(risks)),
        unknowns=list(dict.fromkeys(unknowns)),
        structured_data={
            "risk_search": {
                "cutoff_date": report_date,
                "symbols": scope,
                "hit_counts": hit_counts,
                "sources": sorted({item.source_type for item in evidence}),
            }
        },
    )


def _risk_category(text: str) -> str:
    categories = (
        ("监管/合规风险", ("立案", "调查", "问询", "处罚", "警示函", "监管措施", "ST", "退市")),
        ("股东与资本风险", ("减持", "质押", "冻结", "解禁", "资金占用", "担保")),
        ("业绩与财务风险", ("亏损", "预亏", "下修", "减值", "业绩下降", "净利润下降", "违约", "逾期")),
        ("诉讼与经营风险", ("诉讼", "仲裁", "停产", "事故", "召回", "破产", "重整", "失信", "失联")),
    )
    for label, words in categories:
        if _contains_risk_keyword(text, words):
            return label
    return "潜在利空"


def _normalized_date(value: str) -> str:
    match = re.search(r"(\d{4})[-/年]?(\d{2})[-/月]?(\d{2})", str(value or ""))
    return "-".join(match.groups()) if match else ""


def _tavily_evidence(
    response: dict,
    *,
    symbols: list[str],
    assign_unmatched: bool = False,
) -> list[EvidenceItem]:
    evidence = []
    for result in response.get("results") or []:
        url = str(result.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            url = ""
        searchable = f"{result.get('title') or ''} {result.get('content') or ''} {url}".lower()
        matched_symbols = [symbol for symbol in symbols if symbol.split(".", 1)[0].lower() in searchable]
        if assign_unmatched and not matched_symbols:
            matched_symbols = list(symbols)
        evidence.append(
            EvidenceItem(
                source_type="tavily",
                title=str(result.get("title") or "未命名网页").strip(),
                excerpt=str(result.get("content") or "").strip()[:1200],
                url=url,
                published_at=str(result.get("published_date") or ""),
                symbols=matched_symbols,
            )
        )
    return evidence


def _external_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    lowered = text.lower()
    if any(token in lowered for token in ("ssl", "eof", "timed out", "timeout", "connection reset")):
        return "网络连接中断或超时"
    if any(token in lowered for token in ("403", "forbidden", "拒绝访问")):
        return "来源暂时拒绝访问"
    if any(token in lowered for token in ("permission", "quota", "积分", "权限")):
        return "接口权限不足或额度受限"
    if "404" in lowered:
        return "来源页面不存在"
    return (text or type(exc).__name__)[:160]


def _evidence_is_stale(value: str, *, days: int) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    candidates = [normalized[:10], normalized[:8]]
    for candidate in candidates:
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                published = datetime.strptime(candidate, fmt)
                return published < datetime.now() - timedelta(days=days)
            except ValueError:
                continue
    return False


def _safe_quant_rewrite(candidate: str, fallback: str) -> bool:
    """Reject a model rewrite that adds numbers or drops any discussed symbol."""
    candidate_numbers = set(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", candidate))
    fallback_numbers = set(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", fallback))
    if not candidate_numbers.issubset(fallback_numbers):
        return False
    fallback_symbols = set(re.findall(r"\b\d{6}\.(?:SZ|SH|BJ)\b", fallback, re.I))
    candidate_symbols = set(re.findall(r"\b\d{6}\.(?:SZ|SH|BJ)\b", candidate, re.I))
    return fallback_symbols.issubset(candidate_symbols)


def _contains_risk_keyword(text: str, words: tuple[str, ...]) -> bool:
    if any(word in text for word in words):
        return True
    return re.search(r"(?<![A-Za-z])\*?ST(?![A-Za-z])", text, re.I) is not None

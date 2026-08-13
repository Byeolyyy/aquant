from __future__ import annotations

import threading
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from .integrations import TavilyClient, TushareClient
from .models import AgentContribution, AgentTask, Claim, EvidenceItem, HarnessEvent, ParsedReport, ReportStock, RunPolicy
from .llm import OpenAICompatibleClient
from .repository import Repository
from .public_sources import PublicAStockClient
from .global_markets import GlobalMarketClient
from .agent_prompts import (
    AGENT_PROMPT_IDS,
    AGENT_PROMPTS,
    COORDINATOR_PLANNING_PROMPT,
    RISK_PROMPT,
    PLATFORM_POLICY_PROMPT,
    SYNTHESIS_PROMPT,
)
from .workflows import WORKFLOW_DEFINITIONS, workflow_definition


EventSink = Callable[[HarnessEvent], None]

RISK_KEYWORDS = (
    "减持", "质押", "冻结", "诉讼", "仲裁", "立案", "调查", "问询", "处罚", "警示函",
    "监管措施", "退市", "亏损", "预亏", "下修", "解禁", "担保", "资金占用", "非标意见",
    "风险警示", "违约", "逾期", "停产", "事故", "召回", "减值", "业绩下降", "净利润下降",
    "破产", "重整", "失信", "辞职", "失联",
)


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

    def start(self, report_id: str, policy: RunPolicy | None = None) -> str:
        report = self.repository.get_report(report_id)
        if report is None:
            raise ValueError(f"找不到报告: {report_id}")
        if report.parse_status == "invalid":
            raise ValueError("解析状态为 invalid，不能启动 Agent 分析")
        run_id = str(uuid4())
        self.repository.create_run(run_id, report_id)
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

    def wait(self, run_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(run_id)
        if thread:
            thread.join(timeout)

    def _control(self, run_id: str) -> RunControl:
        control = self._controls.get(run_id)
        if control is None:
            raise ValueError(f"找不到活动运行: {run_id}")
        return control

    def _run(self, run_id: str, report: ParsedReport, policy: RunPolicy) -> None:
        control = self._control(run_id)
        try:
            self.repository.update_run(run_id, "planning")
            self._emit(run_id, "run.status", payload={"status": "planning"})
            selected_agents, selection_rationale = self._select_agents(run_id, report)
            symbols = [row.symbol for row in report.stocks[: policy.max_external_symbols]]
            selected_names = {
                "quant_signal": "量化",
                "company_industry": "公司行业",
                "global_market": "外围市场",
            }
            team_text = "、".join(selected_names.get(agent_id, agent_id) for agent_id in selected_agents)
            self._emit(
                run_id,
                "agent.message",
                agent_id="coordinator",
                payload={
                    "content": f"我已看完报告。先由{team_text} Agent 分头分析，再由风险 Agent 逐票检索近期利空消息，最后由我综合。",
                    "selected_agents": selected_agents,
                    "symbols": symbols,
                    "selection_rationale": selection_rationale,
                    "engine": "openai-compatible" if self.llm_client else "deterministic-demo",
                },
            )
            agent_configs = {
                item["agent_id"]: item for item in self.repository.list_agent_configs()
            }
            workflow_configs = {
                agent_id: workflow_definition(agent_id) for agent_id in selected_agents
            }
            tasks = [
                AgentTask(
                    run_id=run_id,
                    agent_id=agent_id,
                    title=self._task_title(agent_id),
                    instructions=(
                        "基于当前报告完成职责范围内的分析；不得补全缺失事实。"
                        + (
                            "\n本项目附加要求：" + self.repository.agent_custom_instructions(agent_id)
                            if self.repository.agent_custom_instructions(agent_id)
                            else ""
                        )
                    ),
                    symbols=symbols,
                    config_version=int((agent_configs.get(agent_id) or {}).get("config_version") or 1),
                    prompt_version=self._prompt(
                        AGENT_PROMPT_IDS[agent_id], AGENT_PROMPTS[agent_id]
                    )[1],
                    workflow_id=str(workflow_configs[agent_id]["workflow_id"]),
                    workflow_version=int(workflow_configs[agent_id]["version"]),
                )
                for agent_id in selected_agents
            ]
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
                agent_id="coordinator",
                title="吸收复核意见并形成最终综合",
                instructions="区分事实、解释、风险和证据缺口，输出研究解读而非交易建议。",
                symbols=symbols,
                config_version=int((agent_configs.get("coordinator") or {}).get("config_version") or 1),
                prompt_version=self._prompt("coordinator.synthesis", SYNTHESIS_PROMPT)[1],
            )
            self._emit(
                run_id,
                "task.plan",
                agent_id="coordinator",
                payload={
                    "tasks": [task.model_dump(mode="json") for task in tasks],
                    "workflow_steps": [
                        *[task.model_dump(mode="json") for task in tasks],
                        review_task.model_dump(mode="json"),
                        synthesis_task.model_dump(mode="json"),
                    ],
                },
            )
            self.repository.update_run(run_id, "specialists_running")
            self._emit(run_id, "run.status", payload={"status": "specialists_running"})

            contributions: list[AgentContribution] = []
            with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as executor:
                futures = {executor.submit(self._execute_task, task, report, control): task for task in tasks}
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

            self.repository.update_run(run_id, "synthesizing")
            self._emit(run_id, "run.status", payload={"status": "synthesizing"})
            self._emit(
                run_id,
                "agent.message",
                agent_id="coordinator",
                payload={
                    "content": "逐票利空检索已返回。我将合并专业分析与这些有来源的风险消息，形成最终研究综合。",
                    "stage": "synthesis_handoff",
                    "reviewed_by": "risk",
                },
            )
            final = self._synthesize(run_id, report, contributions, review, steering)
            self._emit(run_id, "agent.message", agent_id="coordinator", payload=final)
            self.repository.update_run(run_id, "completed", final)
            self._emit(run_id, "run.completed", payload={"status": "completed", "final": final})
        except Exception as exc:
            self.repository.update_run(run_id, "failed", {"error": str(exc)})
            self._emit(run_id, "run.error", payload={"error": f"{type(exc).__name__}: {exc}"})

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
            if task.agent_id == "quant_signal":
                contribution = self._run_quant_workflow(task, report)
            elif task.agent_id == "company_industry":
                contribution = self._llm_contribution(task, report, self._company_contribution(report))
            elif task.agent_id == "global_market":
                contribution = self._run_global_market_workflow(task, report)
            else:
                contribution = AgentContribution(agent_id=task.agent_id, summary="没有可执行的能力")
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
        formal = [row for row in report.selected_rows if row.reason == "all_conditions_met"]
        formal.sort(
            key=lambda row: (
                -(row.super_net_wanyuan or Decimal("0")),
                -(row.main_net_wanyuan or Decimal("0")),
                -(row.realtime_formula_ratio_pct or Decimal("0")),
                row.pct20 or Decimal("0"),
                row.symbol,
            )
        )
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
            threshold = row.flow_threshold_wanyuan or Decimal("0")
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
                -(item[2].realtime_formula_wanyuan - item[2].flow_threshold_wanyuan)
                if item[0] == 1
                else item[1],
                -(item[2].main_net_wanyuan or Decimal("0")),
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
                        evidence_ids=[report_evidence.evidence_id],
                        confidence="high",
                    )
                )
        else:
            lines.append("\n正式观察：目前没有全部核心条件都通过的标的。")
        if candidates:
            lines.append(f"\n候选观察（{len(candidates)} 只）：")
            for index, (priority, gap, row, failures) in enumerate(candidates, 1):
                surplus = (row.realtime_formula_wanyuan or Decimal("0")) - (row.flow_threshold_wanyuan or Decimal("0"))
                reason = {
                    1: f"资金已超过门槛 {_number(abs(surplus))} 万元，但还差：{'、'.join(failures)}",
                    2: f"其他盘口条件都通过，资金还差 {_number(gap)} 万元",
                    3: f"资金还差 {_number(gap)} 万元，同时最多只差一个盘口条件：{'、'.join(failures) or '无'}",
                }[priority]
                line = f"{index}. {row.symbol}（第 {priority} 优先级）：{reason}。" + _quant_metrics(row)
                if run_id:
                    line += _stability_note(self.repository.signal_stability(row.symbol))
                lines.append(line)
                claims.append(
                    Claim(
                        text=line,
                        kind="interpretation",
                        symbols=[row.symbol],
                        evidence_ids=[report_evidence.evidence_id],
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
        return AgentContribution(
            agent_id="quant_signal",
            summary="\n".join(lines),
            claims=claims,
            evidence=[report_evidence],
            risks=risks,
        )

    def _company_contribution(self, report: ParsedReport) -> AgentContribution:
        evidence: list[EvidenceItem] = []
        claims: list[Claim] = []
        unknowns: list[str] = []
        lines: list[str] = []
        industries: list[str] = []
        snapshots: dict[str, dict] = {}
        if self.tushare_client is not None:
            for stock in report.stocks[:5]:
                snapshots[stock.symbol] = self.tushare_client.company_snapshot(stock.symbol)
        else:
            unknowns.append("Tushare 未配置，公司财务和估值字段无法查询")

        preferred_symbols = [row.symbol for row in report.selected_rows] or [row.symbol for row in report.stocks]
        public_bundles = (
            self.public_a_stock_client.research(preferred_symbols, max_stocks=3)
            if self.public_a_stock_client is not None
            else []
        )
        public_by_symbol = {str(item.get("symbol")): item for item in public_bundles}

        for stock in report.stocks[:5]:
            result = _company_stock_details(
                stock,
                snapshots.get(stock.symbol) or {},
                public_by_symbol.get(stock.symbol) or {},
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
                    symbols=[stock.symbol for stock in report.stocks[:5]],
                )
                evidence.extend(industry_evidence)
                if industry_evidence:
                    lines.append(
                        f"行业补充：围绕 {'、'.join(industries[:3])} 找到 {len(industry_evidence)} 条近期景气、政策或风险资料，详见证据来源。"
                    )
            except Exception as exc:
                unknowns.append("行业补充检索失败：" + _external_error(exc))
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
        for region in ("美国", "韩国"):
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
            lambda: self._search_negative_news(report, scope),
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

    def _search_negative_news(self, report: ParsedReport, scope: list[dict]) -> dict:
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
    ) -> dict[str, object]:
        signal = next((item for item in contributions if item.agent_id == "quant_signal"), None)
        completed_labels = {
            "quant_signal": "量化",
            "company_industry": "公司行业",
            "global_market": "外围市场",
        }
        completed_text = "、".join(
            completed_labels.get(item.agent_id, item.agent_id) for item in contributions
        )
        evidence_count = sum(len(item.evidence) for item in contributions)
        external_count = sum(
            1 for item in contributions for evidence in item.evidence if evidence.source_type != "report"
        )
        fallback: dict[str, object] = {
            "title": "PTrade 多 Agent 研究摘要",
            "report_id": report.report_id,
            "parse_status": report.parse_status,
            "executive_summary": (
                f"本轮已完成{completed_text}分析和风险汇总。"
                f"共登记 {evidence_count} 条资料，其中外部资料 {external_count} 条。"
                + ("报告解析不完整，结论需降低置信度。" if report.parse_status != "valid" else "")
            ),
            "signal_interpretation": signal.summary if signal else "本轮没有量化结果。",
            "risk_notes": review.summary,
            "evidence_gaps": review.unknowns,
            "contributions": [item.model_dump(mode="json") for item in contributions],
            "risk_review": review.model_dump(mode="json"),
            "steering_applied": steering,
            "disclaimer": "仅供研究解读与风险提示，不构成买卖或仓位建议。",
        }
        if self.llm_client is None:
            return fallback
        system = self._prompt("coordinator.synthesis", SYNTHESIS_PROMPT)[0]
        user = json.dumps(
            {
                "report_id": report.report_id,
                "parse_status": report.parse_status,
                "contributions": [item.model_dump(mode="json") for item in contributions],
                "risk_review": review.model_dump(mode="json"),
                "steering": steering,
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            result = self.llm_client.complete_json(system, user)
            data = result.data
            required = {"title", "executive_summary", "signal_interpretation", "risk_notes", "evidence_gaps"}
            if not required.issubset(data):
                raise ValueError("统筹模型输出缺少必需字段")
            data.update(
                {
                    "report_id": report.report_id,
                    "parse_status": report.parse_status,
                    "steering_applied": steering,
                    "disclaimer": "仅供研究解读与风险提示，不构成买卖或仓位建议。",
                    "model": result.model,
                }
            )
            self._emit(
                run_id,
                "model.usage",
                agent_id="coordinator",
                payload={
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return data
        except Exception as exc:
            self._emit(
                run_id,
                "model.fallback",
                agent_id="coordinator",
                payload={"stage": "synthesis", "error": f"{type(exc).__name__}: {exc}"},
            )
            return fallback

    def _select_agents(self, run_id: str, report: ParsedReport) -> tuple[list[str], str]:
        allowed = self.registry.select(report, self.repository)
        if self.llm_client is None:
            return allowed, "本地能力规则根据报告结构选择相关专家。"
        system = self._prompt("coordinator.planning", COORDINATOR_PLANNING_PROMPT)[0]
        compact = {
            "parse_status": report.parse_status,
            "generated_at": report.generated_at,
            "selected_count": len(report.selected_rows),
            "near_count": len(report.near_rows),
            "symbols": [row.symbol for row in report.stocks],
            "diagnostics": report.diagnostics,
            "allowed_agents": allowed,
        }
        try:
            result = self.llm_client.complete_json(system, json.dumps(compact, ensure_ascii=False))
            raw_selected = result.data.get("selected_agents") or []
            model_selected = [agent_id for agent_id in raw_selected if agent_id in allowed]
            # Capability rules define the minimum team. The model may explain the
            # choice, but cannot silently drop a required evidence lane.
            selected = list(dict.fromkeys([*allowed, *model_selected]))
            self._emit(
                run_id,
                "model.usage",
                agent_id="coordinator",
                payload={
                    "stage": "planning",
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
            )
            return selected, str(result.data.get("rationale") or "统筹模型按职责选择了专家。")
        except Exception as exc:
            self._emit(
                run_id,
                "model.fallback",
                agent_id="coordinator",
                payload={"stage": "planning", "error": f"{type(exc).__name__}: {exc}"},
            )
            return allowed, "统筹模型不可用，已回退到本地能力规则。"

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
        user = json.dumps(
            {
                "task": task.model_dump(mode="json"),
                "report": _compact_report(report),
                "deterministic_fallback": fallback.model_dump(mode="json", exclude={"evidence"}),
                "minimum_evidence": [item.model_dump(mode="json") for item in fallback.evidence],
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            result = self.llm_client.complete_json(system, user)
            candidate = AgentContribution.model_validate(result.data)
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
            "global_market": "汇总美股与韩国核心指数走势",
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


def _compact_report(report: ParsedReport) -> dict[str, object]:
    return {
        "report_id": report.report_id,
        "generated_at": report.generated_at,
        "run_slot": report.run_slot,
        "parse_status": report.parse_status,
        "diagnostics": report.diagnostics,
        "stocks": [row.model_dump(mode="json", exclude={"raw_row", "unknown_fields"}) for row in report.stocks],
    }


def _number(value: Decimal | None) -> str:
    if value is None:
        return "缺失"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quant_metrics(row: ReportStock) -> str:
    formula = row.realtime_formula_wanyuan
    threshold = row.flow_threshold_wanyuan
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
        f"大单 {_number(row.large_net_wanyuan)} 万元，主力净额 {_number(row.main_net_wanyuan)} 万元"
    )
    tape = (
        f"量比 {_number(row.vol_ratio)}，换手率 {_number(row.turnover_now_pct)}%，"
        f"外盘内盘条件{'通过' if row.l4_buy_sell is True else '未通过' if row.l4_buy_sell is False else '缺失'}"
    )
    structure = "，资金结构异常" if row.super_large_anomaly is True else ""
    return f"{funding}{ratio}；{order_flow}；{tape}{structure}。"


def _quant_stock_line(row: ReportStock, *, index: int, pool: str) -> str:
    return f"{index}. {row.symbol}（{pool}）：" + _quant_metrics(row)


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


def _company_stock_details(stock: ReportStock, snapshot: dict, public_bundle: dict) -> dict:
    basic = snapshot.get("basic") or {}
    company = snapshot.get("company") or {}
    daily = snapshot.get("daily_basic") or {}
    financial = snapshot.get("financial_indicator") or {}
    forecast = snapshot.get("forecast") or {}
    name = str(basic.get("name") or company.get("com_name") or stock.name or stock.symbol)
    industry = str(basic.get("industry") or "")
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
    if basic or company:
        item = EvidenceItem(
            source_type="tushare",
            title=f"{stock.symbol} {name} · 公司与行业基础资料",
            excerpt="；".join(identity),
            url="https://tushare.pro/",
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
            if item.source_type != "tushare":
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

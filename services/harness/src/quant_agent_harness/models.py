from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


ParseStatus = Literal["valid", "partial", "invalid"]
SourcePool = Literal["selected", "near", "abnormal"]
ReportSource = Literal["manual", "mail"]


class ReportStock(StrictModel):
    symbol: str
    code: str
    name: str = ""
    reason: str = ""
    source_pool: SourcePool
    pct20: Decimal | None = None
    market_cap_yi: Decimal | None = None
    turnover_now_pct: Decimal | None = None
    vol_ratio: Decimal | None = None
    super_net_wanyuan: Decimal | None = None
    large_net_wanyuan: Decimal | None = None
    medium_net_wanyuan: Decimal | None = None
    small_net_wanyuan: Decimal | None = None
    main_net_wanyuan: Decimal | None = None
    realtime_formula_wanyuan: Decimal | None = None
    realtime_formula_ratio_pct: Decimal | None = None
    flow_threshold_wanyuan: Decimal | None = None
    buy_volume: Decimal | None = None
    sell_volume: Decimal | None = None
    l4_buy_sell: bool | None = None
    super_large_anomaly: bool | None = None
    close_pos_in_range: Decimal | None = None
    intraday_strong_ok: bool | None = None
    pass_count: int | None = None
    unmet_items: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    unknown_fields: dict[str, Any] = Field(default_factory=dict)
    raw_row: dict[str, Any] = Field(default_factory=dict)


class ParsedReport(StrictModel):
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    content_hash: str
    raw_text: str
    report_date: str = ""
    generated_at: str = ""
    run_slot: str = ""
    parse_status: ParseStatus
    selected_rows: list[ReportStock] = Field(default_factory=list)
    near_rows: list[ReportStock] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)
    parser_version: str = "quant-agent-v1"
    # 报告来源。这三项存在 payload_json 里，reports 表结构不用动，
    # 旧数据反序列化时吃默认值，桌面老库照常可读。
    source: ReportSource = "manual"
    mail_subject: str = ""
    mail_received_at: str = ""

    @property
    def stocks(self) -> list[ReportStock]:
        return [*self.selected_rows, *self.near_rows]


class AgentProfile(StrictModel):
    agent_id: str
    display_name: str
    lane: str
    description: str
    model_profile_id: str = "demo"
    tool_allowlist: list[str] = Field(default_factory=list)
    enabled: bool = True
    version: int = 1
    required: bool = False


class AgentRuntimeConfig(StrictModel):
    agent_id: str
    enabled: bool = True
    custom_instructions: str = ""
    config_version: int = 1
    updated_at: str = ""


class AgentTask(StrictModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    agent_id: str
    title: str
    instructions: str
    symbols: list[str] = Field(default_factory=list)
    config_version: int = 1
    prompt_version: str = "agent-prompts-v2"
    workflow_id: str = "single-step"
    workflow_version: int = 1


class Claim(StrictModel):
    text: str
    kind: Literal["fact", "interpretation", "risk", "limitation"] = "interpretation"
    symbols: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class EvidenceItem(StrictModel):
    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    source_type: Literal[
        "report", "local_history", "local_stable_master", "tushare", "tavily",
        "official_web", "public_web", "market_data"
    ]
    title: str
    excerpt: str = ""
    url: str = ""
    published_at: str = ""
    retrieved_at: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds")
    )
    symbols: list[str] = Field(default_factory=list)


class AgentContribution(StrictModel):
    agent_id: str
    summary: str
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    follow_up_requests: list[str] = Field(default_factory=list)
    structured_data: dict[str, Any] = Field(default_factory=dict)


class LaneSummary(StrictModel):
    agent_id: str
    lane: str
    headline: str
    key_findings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    structured: dict[str, Any] = Field(default_factory=dict)


class TriggerRecord(StrictModel):
    rule_id: str
    agent_id: str
    fired_at: str
    round: int
    condition_summary: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    chained_from: str = ""
    dispatched: bool = True


class ResearchState(StrictModel):
    """大脑(brain)与颈部(neck/coordinator)之间唯一的交换契约。

    每个字段都是压缩后的结构化研究状态；完整证据仍以 events 与
    AgentContribution 为准。每次更新由颈部发 research.state 事件，
    最终随 run 快照落库（final["research_state"]）。
    """

    round: int = 0
    core_questions: list[str] = Field(default_factory=list)
    lanes: list[LaneSummary] = Field(default_factory=list)
    trigger_log: list[TriggerRecord] = Field(default_factory=list)
    pending_needs: list[dict[str, Any]] = Field(default_factory=list)
    needs_history: list[dict[str, Any]] = Field(default_factory=list)
    conclusions: dict[str, Any] = Field(default_factory=dict)
    terminated_reason: str = ""


class RunPolicy(StrictModel):
    max_rounds: int = 1
    max_brain_rounds: int = 2
    max_event_agent_calls: int = 4
    max_model_calls: int = 12
    max_tool_calls: int = 24
    max_external_symbols: int = 5
    timeout_seconds: int = 480


class HarnessEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    seq: int
    run_id: str
    kind: str
    timestamp: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    agent_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


DEFAULT_AGENT_PROFILES = [
    AgentProfile(
        agent_id="brain",
        display_name="大脑 Agent",
        lane="brain",
        description="纯思考层：初始规划、中期审阅与最终跨域综合（长期/短期利空定性、板块传导判断）；不碰工具、不直接派单。",
        tool_allowlist=[],
        required=True,
    ),
    AgentProfile(
        agent_id="coordinator",
        display_name="统筹 Agent",
        lane="coordination",
        description="秘书/颈部：把大脑的信息需求翻译成任务书，负责派发、去重、预算与结果归档；不做独立判断。",
        tool_allowlist=["report_summary", "task_registry"],
        required=True,
    ),
    AgentProfile(
        agent_id="quant_signal",
        display_name="量化信号 Agent",
        lane="quantitative",
        description="使用确定性计算检查资金、量比、换手和数据质量。",
        tool_allowlist=["report_statistics", "rank_signals"],
        required=True,
    ),
    AgentProfile(
        agent_id="company_industry",
        display_name="公司与行业 Agent",
        lane="fundamental",
        description="研究公司身份、行业、财务和正式公告。",
        tool_allowlist=["stock_identity", "company_profile", "official_announcements"],
    ),
    AgentProfile(
        agent_id="global_market",
        display_name="外围市场 Agent",
        lane="global_market",
        description="汇总美股、韩国与日本核心指数最近交易日走势，并生成可视化。",
        tool_allowlist=["global_index_snapshot"],
    ),
    AgentProfile(
        agent_id="risk",
        display_name="风险 Agent",
        lane="review",
        description="逐只检索近期负面公告与新闻，并总结可核验的潜在利空。",
        tool_allowlist=["official_announcements", "public_news", "tavily_risk_search"],
        required=True,
    ),
    AgentProfile(
        agent_id="capital_trace",
        display_name="资金追查 Agent",
        lane="event_capital",
        description="事件触发：对资金异常标的查询近 10 日资金流历史，区分单日脉冲与持续异动。",
        tool_allowlist=["moneyflow_history", "stock_fund_flow_history"],
    ),
    AgentProfile(
        agent_id="bearish_analysis",
        display_name="利空分析 Agent",
        lane="event_bearish",
        description="事件触发：对风险检索命中的利空事件做针对性补查，并按规则区分结构性（长期）与事件性（短期）影响。",
        tool_allowlist=["risk_targeted_search"],
    ),
    AgentProfile(
        agent_id="global_sector_flow",
        display_name="外围板块资金 Agent",
        lane="event_global_sector",
        description="事件触发：外围指数极端波动时查询行业指数/ETF 涨跌，定位异常板块。",
        tool_allowlist=["global_sector_snapshot"],
    ),
    AgentProfile(
        agent_id="sector_transmission",
        display_name="板块传导映射 Agent",
        lane="event_transmission",
        description="链式触发：把外围异常板块映射到 A 股对应板块并获取板块行情，判定共振或背离。",
        tool_allowlist=["industry_board_quotes"],
    ),
]

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
    kind: Literal["fact", "interpretation", "risk", "limitation"]
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


class RunPolicy(StrictModel):
    max_rounds: int = 1
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
        agent_id="coordinator",
        display_name="统筹 Agent",
        lane="coordination",
        description="理解报告、动态组队、派单、处理插话并形成综合结论。",
        tool_allowlist=["report_summary", "task_registry"],
    ),
    AgentProfile(
        agent_id="quant_signal",
        display_name="量化信号 Agent",
        lane="quantitative",
        description="使用确定性计算检查资金、量比、换手和数据质量。",
        tool_allowlist=["report_statistics", "rank_signals"],
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
    ),
]

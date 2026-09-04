"""确定性事件规则表：代码扳机条件派单。

扳机由代码判定（阈值、布尔字段、风险类别、链式上游产出），模型只能为被
触发的 Agent 写任务书内容，不能决定"要不要查"。引擎保持纯函数：已触发
状态（fired_agents / chained_fired）由 Harness 持有并作为上下文传入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import TriggerRecord

EVENT_SECTOR_MOVE_THRESHOLD_PCT = 2.0
CAPITAL_MULTIPLE_THRESHOLD = 2.0

# 触发器文案用的中文字段标签。
_FIELD_LABELS = {
    "super_large_anomaly": "超大单异常",
    "realtime_formula_wanyuan": "资金公式",
    "effective_threshold": "门槛",
}


@dataclass(frozen=True)
class AnyRowCondition:
    """任一报告行满足条件即触发。

    field 取 ReportStock 字段名；op 支持 eq / gte / lte。
    value 为静态值；field_value_ref 为行内另一字段名（与 value 二选一），
    multiplier 用于"资金公式 ≥ 2×该行有效门槛"这类逐行阈值。
    """

    field: str
    op: str = "eq"
    value: object = None
    field_value_ref: str = ""
    multiplier: float = 1.0
    or_: "AnyRowCondition | None" = None


@dataclass(frozen=True)
class MarketMoveCondition:
    """任一外围指数 |涨跌幅| ≥ threshold_pct。

    require_live=True 时演示兜底数据（market_status != live_delayed）不触发。
    """

    threshold_pct: float = EVENT_SECTOR_MOVE_THRESHOLD_PCT
    require_live: bool = True


@dataclass(frozen=True)
class RiskCategoryCondition:
    """风险审阅命中指定类别之一即触发（类别词表见 harness._risk_category）。"""

    categories: tuple[str, ...]


@dataclass(frozen=True)
class ChainCondition:
    """链式触发：chained_from 规则的产出 structured_data 中该键非空。"""

    structured_key: str


@dataclass(frozen=True)
class EventRule:
    rule_id: str
    agent_id: str
    checkpoints: tuple[str, ...]
    condition: AnyRowCondition | MarketMoveCondition | RiskCategoryCondition | ChainCondition
    description: str
    max_firings_per_run: int = 1
    chained_from: str = ""


@dataclass(frozen=True)
class RuleContext:
    """求值上下文。全部由 Harness 从确定性结果构造，引擎不做任何外部调用。"""

    rows: tuple[dict[str, Any], ...] = ()
    market_status: str = ""
    market_indices: tuple[dict[str, Any], ...] = ()
    risk_categories: tuple[str, ...] = ()
    chained_fired: frozenset[str] = frozenset()
    chained_structured: dict[str, dict[str, Any]] = field(default_factory=dict)
    round: int = 0


EVENT_RULES: list[EventRule] = [
    EventRule(
        rule_id="capital_trace.stock_anomaly",
        agent_id="capital_trace",
        checkpoints=("specialists_done",),
        description="资金异常标的：超大单异常，或资金公式达到该行有效门槛的 2 倍以上。",
        condition=AnyRowCondition(
            field="realtime_formula_wanyuan",
            op="gte",
            field_value_ref="effective_threshold",
            multiplier=CAPITAL_MULTIPLE_THRESHOLD,
            or_=AnyRowCondition(field="super_large_anomaly", op="eq", value=True),
        ),
    ),
    EventRule(
        rule_id="global_sector_flow.move",
        agent_id="global_sector_flow",
        checkpoints=("specialists_done",),
        description="外围指数极端波动：任一指数 |涨跌幅| 达到阈值。",
        condition=MarketMoveCondition(threshold_pct=EVENT_SECTOR_MOVE_THRESHOLD_PCT, require_live=True),
    ),
    EventRule(
        rule_id="bearish_analysis.risk_category",
        agent_id="bearish_analysis",
        checkpoints=("risk_done",),
        description="风险审阅命中关键利空类别，需要判断长期/短期影响。",
        condition=RiskCategoryCondition(
            categories=("监管/合规风险", "股东与资本风险", "业绩与财务风险", "诉讼与经营风险")
        ),
    ),
    EventRule(
        rule_id="sector_transmission.map",
        agent_id="sector_transmission",
        checkpoints=("after_agent:global_sector_flow",),
        description="外围板块资金已定位异常板块，映射到 A 股对应板块。",
        condition=ChainCondition(structured_key="anomaly_sectors"),
        chained_from="global_sector_flow.move",
    ),
]


def _row_hits(row: dict[str, Any], condition: AnyRowCondition | None) -> tuple[bool, str]:
    if condition is None:
        return False, ""
    actual = row.get(condition.field)
    field_label = _FIELD_LABELS.get(condition.field, condition.field)
    symbol = row.get("symbol", "")
    if condition.op == "gte" and condition.field_value_ref:
        threshold = row.get(condition.field_value_ref)
        if actual is None or threshold is None:
            return False, ""
        ref_label = _FIELD_LABELS.get(condition.field_value_ref, condition.field_value_ref)
        limit = float(threshold) * condition.multiplier
        if float(actual) >= limit:
            return (
                True,
                f"{symbol} {field_label} {actual} ≥ {condition.multiplier}×{ref_label} {threshold}",
            )
    if condition.op == "eq":
        if actual is not None and actual == condition.value:
            return True, f"{symbol} {field_label}={actual}"
    if condition.op == "gte" and condition.value is not None:
        if actual is not None and float(actual) >= float(condition.value):
            return True, f"{symbol} {field_label} {actual} ≥ {condition.value}"
    if condition.or_ is not None:
        return _row_hits(row, condition.or_)
    return False, ""


class EventRuleEngine:
    """纯函数求值器：给定 checkpoint 与上下文，返回本轮新触发的记录。"""

    def evaluate(self, checkpoint: str, context: RuleContext) -> list[TriggerRecord]:
        records: list[TriggerRecord] = []
        for rule in EVENT_RULES:
            if checkpoint not in rule.checkpoints:
                continue
            if rule.chained_from and rule.chained_from not in context.chained_fired:
                continue
            record = self._evaluate_rule(rule, context)
            if record is not None:
                records.append(record)
        return records

    def _evaluate_rule(self, rule: EventRule, context: RuleContext) -> TriggerRecord | None:
        condition = rule.condition
        fired_at = datetime.now().astimezone().isoformat(timespec="seconds")

        if isinstance(condition, AnyRowCondition):
            hits: list[tuple[dict[str, Any], str]] = []
            for row in context.rows:
                matched, summary = _row_hits(row, condition)
                if matched:
                    hits.append((dict(row), summary))
            if not hits:
                return None
            return TriggerRecord(
                rule_id=rule.rule_id,
                agent_id=rule.agent_id,
                fired_at=fired_at,
                round=context.round,
                condition_summary="、".join(summary for _row, summary in hits),
                inputs={"rows": [row for row, _summary in hits]},
            )

        if isinstance(condition, MarketMoveCondition):
            if condition.require_live and context.market_status not in ("live_delayed", "live"):
                return None
            extreme = [
                item
                for item in context.market_indices
                if item.get("change_percent") is not None
                and abs(float(item["change_percent"])) >= condition.threshold_pct
            ]
            if not extreme:
                return None
            summaries = [
                f"{item.get('name', item.get('symbol', ''))} {float(item['change_percent']):+.2f}%"
                for item in extreme
            ]
            return TriggerRecord(
                rule_id=rule.rule_id,
                agent_id=rule.agent_id,
                fired_at=fired_at,
                round=context.round,
                condition_summary="外围指数极端波动（|涨跌幅|≥"
                + f"{condition.threshold_pct:.1f}%）：" + "、".join(summaries),
                inputs={"market_indices": [dict(item) for item in extreme]},
            )

        if isinstance(condition, RiskCategoryCondition):
            hit = [c for c in context.risk_categories if c in condition.categories]
            if not hit:
                return None
            return TriggerRecord(
                rule_id=rule.rule_id,
                agent_id=rule.agent_id,
                fired_at=fired_at,
                round=context.round,
                condition_summary="风险审阅命中关键利空类别：" + "、".join(hit),
                inputs={"risk_categories": list(hit)},
            )

        if isinstance(condition, ChainCondition):
            upstream = context.chained_structured.get(rule.chained_from, {})
            payload = upstream.get(condition.structured_key) or []
            if not payload:
                return None
            return TriggerRecord(
                rule_id=rule.rule_id,
                agent_id=rule.agent_id,
                fired_at=fired_at,
                round=context.round,
                condition_summary="外围板块资金已定位异常板块：" + "、".join(
                    f"{item.get('name', '')} {float(item['change_percent']):+.2f}%" for item in payload
                ),
                inputs={"anomaly_sectors": [dict(item) for item in payload]},
                chained_from=rule.chained_from,
            )

        return None


# 美股 SPDR 行业 ETF → A 股板块名称关键词（板块传导映射 Agent 使用）
SECTOR_TRANSMISSION_MAP: dict[str, dict[str, Any]] = {
    "XLF": {"name": "金融", "a_share_boards": ["银行", "证券", "保险", "多元金融"]},
    "XLK": {"name": "科技", "a_share_boards": ["半导体", "消费电子", "电子", "计算机", "通信", "软件"]},
    "XLE": {"name": "能源", "a_share_boards": ["石油", "煤炭", "油气"]},
    "XLV": {"name": "医疗", "a_share_boards": ["医药", "医疗", "生物"]},
    "XLY": {"name": "可选消费", "a_share_boards": ["汽车", "家电", "社会服务", "零售", "旅游"]},
    "XLP": {"name": "必选消费", "a_share_boards": ["食品饮料", "农林牧渔", "白酒"]},
    "XLI": {"name": "工业", "a_share_boards": ["机械", "军工", "电力设备", "自动化"]},
    "XLB": {"name": "材料", "a_share_boards": ["化工", "有色", "钢铁", "稀土"]},
    "XLRE": {"name": "房地产", "a_share_boards": ["房地产", "建筑", "建材"]},
    "XLU": {"name": "公用事业", "a_share_boards": ["电力", "公用事业", "燃气"]},
}

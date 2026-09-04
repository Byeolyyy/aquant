from __future__ import annotations

from typing import Any


WORKFLOW_DEFINITIONS: dict[str, dict[str, Any]] = {
    "quant_signal": {
        "workflow_id": "quant-signal-subgraph",
        "version": 1,
        "mode": "deterministic_workflow",
        "description": "证券身份解析、确定性规则计算、历史稳定性记忆、资金结构可视化与模型解释。",
        "nodes": [
            {"node_id": "identity_sync", "name": "证券身份解析", "kind": "database"},
            {"node_id": "signal_rules", "name": "正式池与 P1/P2/P3 计算", "kind": "deterministic"},
            {"node_id": "stability_memory", "name": "稳定性写入与历史统计", "kind": "memory"},
            {"node_id": "visual_payload", "name": "资金结构可视化数据", "kind": "visualization"},
            {"node_id": "plain_explanation", "name": "通俗解释与结构校验", "kind": "model_optional"},
        ],
    },
    "global_market": {
        "workflow_id": "global-market-subgraph",
        "version": 1,
        "mode": "market_snapshot_workflow",
        "description": "确定最近有效交易日、读取美韩核心指数、标准化涨跌并生成可视化数据。",
        "nodes": [
            {"node_id": "session_scope", "name": "交易日与时区口径", "kind": "deterministic"},
            {"node_id": "index_fetch", "name": "美韩指数延迟行情", "kind": "tool"},
            {"node_id": "normalize_returns", "name": "涨跌幅标准化", "kind": "deterministic"},
            {"node_id": "visual_payload", "name": "可视化数据生成", "kind": "visualization"},
            {"node_id": "market_explanation", "name": "市场分化通俗解释", "kind": "model_optional"},
        ],
    },
    "risk": {
        "workflow_id": "negative-news-risk-subgraph",
        "version": 1,
        "mode": "evidence_first_search_workflow",
        "description": "逐票确定检索范围，查询公告与新闻，过滤报告日后的资料，再总结潜在利空。",
        "nodes": [
            {"node_id": "risk_scope", "name": "逐票检索范围", "kind": "deterministic"},
            {"node_id": "negative_news_search", "name": "公告与负面新闻检索", "kind": "tool"},
            {"node_id": "evidence_filter", "name": "利空证据与日期过滤", "kind": "deterministic"},
            {"node_id": "risk_summary", "name": "逐票通俗总结", "kind": "model_optional"},
        ],
    },
    "capital_trace": {
        "workflow_id": "capital-trace-subgraph",
        "version": 1,
        "mode": "event_triggered_workflow",
        "description": "事件触发：锁定资金异常标的，查询近 10 日资金流历史，区分单日脉冲与持续异动。",
        "nodes": [
            {"node_id": "trigger_scope", "name": "资金异常标的锁定", "kind": "deterministic"},
            {"node_id": "flow_history", "name": "资金流历史查询", "kind": "tool"},
            {"node_id": "pulse_classify", "name": "脉冲/持续性分类", "kind": "deterministic"},
            {"node_id": "capital_explanation", "name": "资金行为通俗解释", "kind": "model_optional"},
        ],
    },
    "bearish_analysis": {
        "workflow_id": "bearish-analysis-subgraph",
        "version": 1,
        "mode": "event_triggered_workflow",
        "description": "事件触发：对命中关键类别的利空做针对性补查，并按规则区分结构性（长期）与事件性（短期）。",
        "nodes": [
            {"node_id": "risk_scope", "name": "利空类别与标的锁定", "kind": "deterministic"},
            {"node_id": "targeted_search", "name": "定向利空补查", "kind": "tool"},
            {"node_id": "duration_rubric", "name": "长期/短期判定", "kind": "deterministic"},
            {"node_id": "bearish_explanation", "name": "持续性与影响范围解释", "kind": "model_optional"},
        ],
    },
    "global_sector_flow": {
        "workflow_id": "global-sector-flow-subgraph",
        "version": 1,
        "mode": "event_triggered_workflow",
        "description": "事件触发：外围极端波动时查询行业 ETF 涨跌，定位异常板块。",
        "nodes": [
            {"node_id": "session_scope", "name": "交易日与报告日锚点", "kind": "deterministic"},
            {"node_id": "sector_fetch", "name": "行业 ETF 行情", "kind": "tool"},
            {"node_id": "anomaly_sort", "name": "异常板块定位", "kind": "deterministic"},
            {"node_id": "sector_explanation", "name": "板块分化通俗解释", "kind": "model_optional"},
        ],
    },
    "sector_transmission": {
        "workflow_id": "sector-transmission-subgraph",
        "version": 1,
        "mode": "event_triggered_chain_workflow",
        "description": "链式触发：把外围异常板块映射到 A 股对应板块并获取板块行情，判定共振或背离。",
        "nodes": [
            {"node_id": "mapping_resolve", "name": "板块映射解析", "kind": "deterministic"},
            {"node_id": "board_fetch", "name": "A 股板块行情", "kind": "tool"},
            {"node_id": "transmission_rank", "name": "共振/背离判定", "kind": "deterministic"},
            {"node_id": "transmission_explanation", "name": "传导分析解释", "kind": "model_optional"},
        ],
    },
}


def workflow_definition(agent_id: str) -> dict[str, Any]:
    return WORKFLOW_DEFINITIONS.get(
        agent_id,
        {
            "workflow_id": f"{agent_id}-single-step",
            "version": 1,
            "mode": "single_step",
            "description": "当前使用单步专业分析，后续可升级为独立子图。",
            "nodes": [{"node_id": "analysis", "name": "专业分析", "kind": "agent"}],
        },
    )

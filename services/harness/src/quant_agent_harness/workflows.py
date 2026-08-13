from __future__ import annotations

from typing import Any


WORKFLOW_DEFINITIONS: dict[str, dict[str, Any]] = {
    "quant_signal": {
        "workflow_id": "quant-signal-subgraph",
        "version": 1,
        "mode": "deterministic_workflow",
        "description": "证券身份解析、确定性规则计算、历史稳定性记忆与模型解释。",
        "nodes": [
            {"node_id": "identity_sync", "name": "证券身份解析", "kind": "database"},
            {"node_id": "signal_rules", "name": "正式池与 P1/P2/P3 计算", "kind": "deterministic"},
            {"node_id": "stability_memory", "name": "稳定性写入与历史统计", "kind": "memory"},
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

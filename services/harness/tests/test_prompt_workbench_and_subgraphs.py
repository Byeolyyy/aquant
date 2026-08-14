from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from quant_agent_harness.agent_prompts import PLATFORM_POLICY_PROMPT
from quant_agent_harness.harness import Harness, _compact_report
from quant_agent_harness.global_markets import GlobalMarketClient
from quant_agent_harness.local_knowledge import LocalKnowledgeIndex
from quant_agent_harness.models import EvidenceItem
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository
from quant_agent_harness.server import ProtocolServer


RAW = """生成时间: 2026-07-31 14:30:00
selected_head:
symbol name reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS 浦发银行 all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""


class PromptWorkbenchAndSubgraphTests(unittest.TestCase):
    def test_global_market_client_uses_explicit_demo_fallback(self):
        client = GlobalMarketClient(demo_fallback=True)
        client._fetch_one = lambda _spec, _date: (_ for _ in ()).throw(RuntimeError("offline"))  # type: ignore[method-assign]
        snapshot = client.snapshot("2026-07-31 14:30:00")
        self.assertEqual(snapshot["status"], "demo_fallback")
        self.assertEqual(len(snapshot["market_indices"]), 5)
        self.assertIn("不代表真实市场行情", snapshot["notice"])
        us_dates = {
            item["trade_date"] for item in snapshot["market_indices"] if item["region"] == "美国"
        }
        korea_dates = {
            item["trade_date"] for item in snapshot["market_indices"] if item["region"] == "韩国"
        }
        self.assertEqual(us_dates, {"2026-07-30"})
        self.assertEqual(korea_dates, {"2026-07-31"})
        self.assertEqual(snapshot["a_share_report_date"], "2026-07-31")

    def test_prompt_draft_publish_and_rollback_are_versioned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            original, original_version = repository.published_prompt("global_market.system", "")
            replacement = "你是外围市场 Agent。只陈述结构化指数数据，区分交易日期与当地时区，并清楚说明演示数据。" * 2

            draft_id = repository.create_prompt_draft(
                "global_market.system", replacement, "测试发布链路"
            )
            self.assertEqual(repository.published_prompt("global_market.system", "")[0], original)
            repository.publish_prompt_version(draft_id)
            self.assertEqual(repository.published_prompt("global_market.system", "")[0], replacement)

            workspace = repository.prompt_workspace()
            market = next(item for item in workspace if item["prompt_id"] == "global_market.system")
            original_id = next(
                version["version_id"]
                for version in market["versions"]
                if version["version_number"] == original_version
            )
            repository.rollback_prompt_version("global_market.system", original_id)
            rolled_back, rolled_back_version = repository.published_prompt("global_market.system", "")
            self.assertEqual(rolled_back, original)
            self.assertEqual(rolled_back_version, 3)

    def test_quant_prompt_is_exposed_as_editable_strategy_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            workspace = repository.prompt_workspace()
            quant = next(item for item in workspace if item["prompt_id"] == "quant_signal.system")
            published = next(item for item in quant["versions"] if item["status"] == "published")

            self.assertEqual(quant["name"], "量化策略模板")
            self.assertEqual(
                quant["template_sections"],
                [
                    "策略基本信息",
                    "输入字段字典",
                    "核心条件",
                    "信号分层规则",
                    "单只标的解释顺序",
                    "缺失值与冲突处理",
                ],
            )
            self.assertIn("【策略配置区：用户可修改】", published["content"])
            self.assertIn("P1 候选", quant["starter_content"])

    def test_untouched_legacy_quant_prompt_is_migrated_without_overwriting_user_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.sqlite"
            Repository(db_path)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("DELETE FROM prompt_versions WHERE prompt_id='quant_signal.system'")
                connection.execute(
                    """
                    INSERT INTO prompt_versions
                        (version_id, prompt_id, version_number, content, status, change_note, published_at)
                    VALUES ('legacy-quant-v1', 'quant_signal.system', 1,
                            '旧版量化提示词，仅解释固定字段和候选结果。',
                            'published', '系统初始版本', CURRENT_TIMESTAMP)
                    """
                )
                connection.commit()
            finally:
                connection.close()

            repository = Repository(db_path)
            quant = next(
                item for item in repository.prompt_workspace()
                if item["prompt_id"] == "quant_signal.system"
            )
            published = next(item for item in quant["versions"] if item["status"] == "published")
            legacy = next(item for item in quant["versions"] if item["version_id"] == "legacy-quant-v1")

            self.assertEqual(published["version_number"], 2)
            self.assertIn("【策略配置区：用户可修改】", published["content"])
            self.assertEqual(legacy["status"], "archived")

    def test_untouched_legacy_synthesis_prompt_is_migrated_to_rule_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.sqlite"
            Repository(db_path)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("DELETE FROM prompt_versions WHERE prompt_id='coordinator.synthesis'")
                connection.execute(
                    """
                    INSERT INTO prompt_versions
                        (version_id, prompt_id, version_number, content, status, change_note, published_at)
                    VALUES ('legacy-synthesis-v1', 'coordinator.synthesis', 1,
                            '旧版统筹提示词，泛化总结全部 Agent 的工作过程。',
                            'published', '系统初始版本', CURRENT_TIMESTAMP)
                    """
                )
                connection.commit()
            finally:
                connection.close()

            repository = Repository(db_path)
            synthesis = next(
                item for item in repository.prompt_workspace()
                if item["prompt_id"] == "coordinator.synthesis"
            )
            published = next(item for item in synthesis["versions"] if item["status"] == "published")
            legacy = next(
                item for item in synthesis["versions"]
                if item["version_id"] == "legacy-synthesis-v1"
            )

            self.assertEqual(published["version_number"], 2)
            self.assertIn("【最终输出目标：规则推荐卡片】", published["content"])
            self.assertEqual(legacy["status"], "archived")

    def test_quant_strategy_inputs_keep_custom_report_fields(self):
        report = parse_ptrade_report(
            RAW.replace(
                "l4_buy_sell\n",
                "l4_buy_sell custom_momentum_score\n",
            ).replace(
                "2.5 True\n",
                "2.5 True 87.5\n",
            )
        )
        compact = _compact_report(report, include_unknown=True)
        stocks = compact["stocks"]

        self.assertEqual(stocks[0]["unknown_fields"]["custom_momentum_score"], "87.5")

    def test_platform_policy_is_locked_and_always_bound(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            with self.assertRaisesRegex(ValueError, "只读"):
                repository.create_prompt_draft("platform.policy", "不能覆盖的平台规则" * 20)
            effective, label = Harness(repository)._prompt("global_market.system", "fallback")
            self.assertIn(PLATFORM_POLICY_PROMPT, effective)
            self.assertIn("platform.policy:v1+global_market.system:v1", label)

    def test_market_knowledge_is_automatically_deduplicated_and_retrievable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            index = LocalKnowledgeIndex(repository)
            evidence = EvidenceItem(
                source_type="official_web",
                title="浦发银行发布年度经营公告",
                excerpt="公告披露净利润增长，资产质量指标保持稳定。",
                url="https://example.com/600000",
                symbols=["600000.SS"],
            )
            self.assertEqual(index.index_evidence([evidence]), 1)
            self.assertEqual(index.index_evidence([evidence]), 0)
            hits = index.search("浦发银行经营情况和资产质量", symbols=["600000.SS"])
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].source_type, "local_history")
            self.assertEqual(repository.knowledge_stats()["document_count"], 1)

    def test_quant_subgraph_records_stability_and_emits_node_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            events = []
            harness = Harness(repository, events.append)
            for minute in ("30", "31"):
                report = parse_ptrade_report(RAW.replace("14:30:00", f"14:{minute}:00"))
                repository.save_report(report)
                run_id = harness.start(report.report_id)
                harness.wait(run_id, timeout=5)
            stability = repository.signal_stability("600000.SS")
            self.assertEqual(stability["sample_count"], 2)
            self.assertEqual(stability["formal_count"], 2)
            quant_nodes = [
                event
                for event in events
                if event.kind == "workflow.node" and event.agent_id == "quant_signal"
            ]
            self.assertTrue(any(event.payload.get("status") == "started" for event in quant_nodes))
            self.assertTrue(any(event.payload.get("status") == "completed" for event in quant_nodes))

    def test_protocol_exposes_prompt_workspace_and_demo_workflows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = ProtocolServer(Repository(Path(temp_dir) / "test.sqlite"))
            prompts = server.handle("get_prompt_workspace", {})["prompts"]
            self.assertGreaterEqual(len(prompts), 7)
            self.assertNotIn("market_event.system", {prompt["prompt_id"] for prompt in prompts})
            self.assertIn("risk.negative_news.system", {prompt["prompt_id"] for prompt in prompts})
            agent_ids = {agent["agent_id"] for agent in server.handle("get_agents", {})["agents"]}
            self.assertIn("global_market", agent_ids)
            self.assertIn("risk", agent_ids)
            self.assertNotIn("market_event", agent_ids)
            workflows = server.handle("get_workflows", {})["workflows"]
            self.assertEqual(
                {workflow["workflow_id"] for workflow in workflows},
                {"quant-signal-subgraph", "global-market-subgraph", "negative-news-risk-subgraph"},
            )


if __name__ == "__main__":
    unittest.main()

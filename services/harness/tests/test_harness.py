from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quant_agent_harness.harness import Harness
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository


RAW = """生成时间: 2026-07-31 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""


class FakeTushare:
    def company_snapshot(self, symbol):
        return {
            "symbol": symbol,
            "ts_code": "600000.SH",
            "basic": {
                "name": "浦发银行",
                "industry": "银行",
                "market": "主板",
                "list_date": "19991110",
            },
            "company": {"province": "上海", "city": "上海"},
            "daily_basic": {"trade_date": "20260720", "close": 10.5, "pe": 6.2},
            "errors": [],
        }


class FakeTavily:
    def search(self, query, **_kwargs):
        if "利空" in query:
            return {
                "query": query,
                "results": [
                    {
                        "title": "浦发银行收到监管警示函",
                        "url": "https://example.com/risk-source",
                        "content": "公告显示公司收到监管警示函，具体影响需核验原文。",
                        "published_date": "2026-07-29",
                    },
                    {
                        "title": "浦发银行报告日后收到处罚",
                        "url": "https://example.com/future-risk-source",
                        "content": "这条资料晚于报告日期，不应进入本轮分析。",
                        "published_date": "2026-08-01",
                    }
                ],
            }
        return {
            "query": query,
            "results": [
                {
                    "title": "浦发银行近期公开资料",
                    "url": "https://example.com/source",
                    "content": "这是一条带来源的市场资料摘要。",
                    "published_date": "2026-07-20",
                }
            ],
        }


class FakeGlobalMarket:
    def snapshot(self, as_of=None):
        self.as_of = as_of
        return {
            "status": "live_delayed",
            "provider": "fake delayed feed",
            "retrieved_at": "2026-07-31T18:00:00+08:00",
            "notice": "测试延迟行情",
            "errors": [],
            "quality_flags": [],
            "market_indices": [
                {
                    "ticker": "^GSPC", "name": "标普 500", "region": "美国", "currency": "USD",
                    "trade_date": "2026-07-30", "timezone": "America/New_York", "close": 6300.0,
                    "previous_close": 6270.0, "change": 30.0, "change_percent": 0.48,
                    "history": [{"date": "2026-07-29", "close": 6270.0}, {"date": "2026-07-30", "close": 6300.0}],
                    "source_url": "https://example.com/gspc",
                },
                {
                    "ticker": "^KS11", "name": "KOSPI", "region": "韩国", "currency": "KRW",
                    "trade_date": "2026-07-31", "timezone": "Asia/Seoul", "close": 3250.0,
                    "previous_close": 3260.0, "change": -10.0, "change_percent": -0.31,
                    "history": [{"date": "2026-07-30", "close": 3260.0}, {"date": "2026-07-31", "close": 3250.0}],
                    "source_url": "https://example.com/kospi",
                },
            ],
        }


class HarnessTests(unittest.TestCase):
    def test_dynamic_team_and_completed_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            harness = Harness(repository, events.append)
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=5)
            snapshot = repository.run_snapshot(run_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["status"], "completed")
            plan = next(event for event in events if event.kind == "task.plan")
            agent_ids = {task["agent_id"] for task in plan.payload["tasks"]}
            self.assertIn("quant_signal", agent_ids)
            self.assertNotIn("history_pattern", agent_ids)
            self.assertIn("company_industry", agent_ids)
            workflow_agents = [step["agent_id"] for step in plan.payload["workflow_steps"]]
            self.assertEqual(workflow_agents[-2:], ["risk", "coordinator"])
            coordinator_stages = [
                event.payload.get("stage")
                for event in events
                if event.kind == "agent.message" and event.agent_id == "coordinator"
            ]
            self.assertIn("risk_review_handoff", coordinator_stages)
            self.assertNotIn("synthesis_handoff", coordinator_stages)
            self.assertEqual(events[-1].kind, "run.completed")

    def test_history_agent_is_never_dispatched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            prior = parse_ptrade_report(RAW)
            current = parse_ptrade_report(RAW.replace("14:30:00", "14:31:00"))
            repository.save_report(prior)
            repository.save_report(current)
            events = []
            harness = Harness(repository, events.append)
            run_id = harness.start(current.report_id)
            harness.wait(run_id, timeout=5)
            plan = next(event for event in events if event.kind == "task.plan")
            agent_ids = {task["agent_id"] for task in plan.payload["tasks"]}
            self.assertNotIn("history_pattern", agent_ids)
            self.assertFalse(any(event.agent_id == "history_pattern" for event in events))

    def test_quant_agent_uses_ptrade_formal_and_three_candidate_rules(self):
        raw = """生成时间: 2026-07-21 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell super_large_anomaly super_net_wanyuan large_net_wanyuan main_net_wanyuan
600000.SS all_conditions_met 4300 4000 1.2 2.5 True False 900 300 1200
near_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell super_large_anomaly super_net_wanyuan large_net_wanyuan main_net_wanyuan
000001.SZ near_miss 4300 4000 1.0 3.1 True False 500 200 700
000002.SZ near_miss 3600 4000 1.2 3.1 True False 450 180 630
000003.SZ near_miss 3200 4000 1.2 3.1 False False 400 160 560"""
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            contribution = Harness(repository)._quant_contribution(parse_ptrade_report(raw))
        self.assertIn("正式观察（1 只", contribution.summary)
        self.assertIn("第 1 优先级", contribution.summary)
        self.assertIn("第 2 优先级", contribution.summary)
        self.assertIn("第 3 优先级", contribution.summary)
        self.assertIn("资金公式 4300 万元，比门槛高 300 万元", contribution.summary)
        self.assertIn("超大单 900 万元", contribution.summary)

    def test_configured_external_agents_register_traceable_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            harness = Harness(
                repository,
                events.append,
                None,
                FakeTushare(),  # type: ignore[arg-type]
                FakeTavily(),  # type: ignore[arg-type]
                None,
                FakeGlobalMarket(),  # type: ignore[arg-type]
            )
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=5)
            company = next(
                event
                for event in events
                if event.kind == "agent.message" and event.agent_id == "company_industry"
            )
            global_market = next(
                event
                for event in events
                if event.kind == "agent.message" and event.agent_id == "global_market"
            )
            review = next(
                event
                for event in events
                if event.kind == "agent.message" and event.agent_id == "risk"
            )
            self.assertEqual(company.payload["evidence"][0]["source_type"], "tushare")
            self.assertIn("浦发银行", company.payload["summary"])
            self.assertEqual(global_market.payload["evidence"][0]["source_type"], "market_data")
            self.assertIn("标普 500", global_market.payload["summary"])
            self.assertEqual(len(global_market.payload["structured_data"]["market_indices"]), 2)
            self.assertEqual(global_market.payload["structured_data"]["market_indices"][0]["trade_date"], "2026-07-30")
            self.assertEqual(global_market.payload["structured_data"]["market_indices"][1]["trade_date"], "2026-07-31")
            self.assertIn("逐票利空检索", review.payload["summary"])
            self.assertIn("监管警示函", review.payload["summary"])
            self.assertNotIn("报告日后收到处罚", review.payload["summary"])
            self.assertEqual(review.payload["evidence"][0]["source_type"], "tavily")
            self.assertEqual(review.payload["evidence"][0]["symbols"], ["600000.SS"])


if __name__ == "__main__":
    unittest.main()

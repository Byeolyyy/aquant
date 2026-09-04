from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fakes import FakeGlobalMarket, FakePublicAStock
from quant_agent_harness.harness import Harness
from quant_agent_harness.llm import ModelResult
from quant_agent_harness.models import RunPolicy
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository
from test_harness import FakeTavily

RAW = """生成时间: 2026-07-31 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""

RAW_ANOMALY = """生成时间: 2026-07-31 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell super_large_anomaly
600000.SS all_conditions_met 4300 4000 1.2 2.5 True True
near_head: empty"""

RAW_OVER_THRESHOLD = RAW.replace(
    "600000.SS all_conditions_met 4300 4000 1.2 2.5 True",
    "600000.SS all_conditions_met 9000 4000 1.2 2.5 True",
)


class BrainPlanningModel:
    model = "brain-planning-fake"

    def complete_json(self, system: str, user: str) -> ModelResult:
        if "core_questions" in system:
            return ModelResult(
                data={
                    "research_strategy": "常规分析之外，重点核验公司公告与资金行为。",
                    "core_questions": ["量化信号是否可靠？"],
                    "agent_calls": [
                        {
                            "agent_id": "company_industry",
                            "question": "核验公司最近是否有重大公告",
                            "symbols": ["600000.SS"],
                            "reason": "补充背景",
                            "priority": 2,
                        },
                        {
                            "agent_id": "capital_trace",
                            "question": "核查资金公式异常标的的资金流历史",
                            "symbols": ["600000.SS"],
                            "reason": "资金行为影响信号可信度",
                            "priority": 1,
                        },
                    ],
                },
                model=self.model,
            )
        raise RuntimeError("其他阶段故意回退到确定性输出")


class AlwaysContinueModel:
    """每次都提出一个不同的问题，验证大脑循环被 max_brain_rounds 截断。"""

    model = "brain-loop-fake"

    def __init__(self):
        self.review_calls = 0

    def complete_json(self, system: str, user: str) -> ModelResult:
        if "现在不是做最终总结" in system:
            self.review_calls += 1
            return ModelResult(
                data={
                    "decision": "continue",
                    "review_summary": "第 %d 次审阅仍认为需要补充信息。" % self.review_calls,
                    "agent_calls": [
                        {
                            "agent_id": "company_industry",
                            "question": "请补充核验公司公告第 %d 项。" % self.review_calls,
                            "symbols": ["600000.SS"],
                            "reason": "持续补充",
                            "priority": 1,
                        }
                    ],
                },
                model=self.model,
            )
        raise RuntimeError("其他阶段故意回退到确定性输出")


class PlanCapitalForNearModel:
    """规划阶段点名资金追查查一只规则未触发的标的。"""

    model = "brain-plan-capital-fake"

    def complete_json(self, system: str, user: str) -> ModelResult:
        if "core_questions" in system:
            return ModelResult(
                data={
                    "research_strategy": "核查近池标的的资金行为。",
                    "core_questions": ["量化信号是否可靠？"],
                    "agent_calls": [
                        {
                            "agent_id": "capital_trace",
                            "question": "请核查 000001.SZ 近 10 日主力资金流向并判断持续性。",
                            "symbols": ["000001.SZ"],
                            "reason": "资金行为影响信号可信度",
                            "priority": 1,
                        }
                    ],
                },
                model=self.model,
            )
        raise RuntimeError("其他阶段故意回退到确定性输出")


class ReviewEventAgentModel:
    """大脑审阅时自主点名事件 Agent（资金追查），验证审阅环可以调动专项力量。"""

    model = "brain-review-event-fake"

    def __init__(self):
        self.review_calls = 0

    def complete_json(self, system: str, user: str) -> ModelResult:
        if "现在不是做最终总结" in system:
            self.review_calls += 1
            if self.review_calls == 1:
                return ModelResult(
                    data={
                        "decision": "continue",
                        "review_summary": "资金公式偏高，需要核查资金流历史。",
                        "agent_calls": [
                            {
                                "agent_id": "capital_trace",
                                "question": "请核查 600000.SS 近 10 日主力资金流向并判断持续性。",
                                "symbols": ["600000.SS"],
                                "reason": "资金行为影响信号可信度",
                                "priority": 1,
                            }
                        ],
                    },
                    model=self.model,
                )
            return ModelResult(
                data={
                    "decision": "finish",
                    "review_summary": "专项调查已完成，可以收尾。",
                    "agent_calls": [],
                },
                model=self.model,
            )
        raise RuntimeError("其他阶段故意回退到确定性输出")


class BrainTests(unittest.TestCase):
    def test_brain_planning_deterministic_fallback_without_llm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            harness = Harness(repository, events.append, global_market_client=FakeGlobalMarket())
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=10)

            snapshot = repository.run_snapshot(run_id)
            self.assertEqual(snapshot["status"], "completed")
            state_events = [event for event in events if event.kind == "research.state"]
            self.assertGreaterEqual(len(state_events), 3)
            final_state = snapshot["final"]["research_state"]
            self.assertTrue(final_state["core_questions"])
            self.assertEqual(final_state["terminated_reason"], "brain_finish")
            self.assertEqual(final_state["needs_history"], [])
            # 确定性跨域判断逐票落盘：无模型时每票仍有资金行为/利空定性/板块传导占位
            cross_domain = snapshot["final"]["cross_domain"]
            self.assertEqual([item["symbol"] for item in cross_domain], ["600000.SS"])
            self.assertIn("capital_behavior", cross_domain[0])

    def test_brain_planning_model_merges_calls_and_dispatches_event_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            harness = Harness(
                repository,
                events.append,
                BrainPlanningModel(),  # type: ignore[arg-type]
                public_a_stock_client=FakePublicAStock(),  # type: ignore[arg-type]
                global_market_client=FakeGlobalMarket(),  # type: ignore[arg-type]
            )
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=10)

            plan = next(event for event in events if event.kind == "task.plan")
            tasks = {task["agent_id"]: task for task in plan.payload["tasks"]}
            self.assertIn("company_industry", tasks)
            self.assertIn("大脑专项指令：核验公司最近是否有重大公告", tasks["company_industry"]["instructions"])
            opening = next(
                event
                for event in events
                if event.kind == "agent.message" and event.agent_id == "coordinator"
            )
            self.assertEqual(opening.payload["core_questions"], ["量化信号是否可靠？"])
            # 大脑在规划阶段点名了资金追查（即使规则未触发）→ 秘书派发专项调查
            brain_dispatch = [event for event in events if event.kind == "task.brain_dispatch"]
            self.assertEqual(len(brain_dispatch), 1)
            self.assertEqual(brain_dispatch[0].payload["requested_by"], "brain")
            capital_messages = [
                event for event in events
                if event.kind == "agent.message" and event.agent_id == "capital_trace"
            ]
            self.assertEqual(len(capital_messages), 1)
            self.assertEqual(capital_messages[0].payload["stage"], "brain_dispatch_result")
            # 大脑点名的专项 Agent 记录进 trigger_log 与最终跨域判断
            snapshot = repository.run_snapshot(run_id)
            cross_domain = snapshot["final"]["cross_domain"]
            self.assertEqual([item["symbol"] for item in cross_domain], ["600000.SS"])

    def test_brain_review_can_dispatch_event_agent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            model = ReviewEventAgentModel()
            harness = Harness(
                repository,
                events.append,
                model,  # type: ignore[arg-type]
                public_a_stock_client=FakePublicAStock(),  # type: ignore[arg-type]
                global_market_client=FakeGlobalMarket(),  # type: ignore[arg-type]
            )
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=10)

            snapshot = repository.run_snapshot(run_id)
            self.assertEqual(snapshot["status"], "completed")
            capital_messages = [
                event for event in events
                if event.kind == "agent.message" and event.agent_id == "capital_trace"
            ]
            self.assertEqual(len(capital_messages), 1)
            replans = [event for event in events if event.kind == "task.replan"]
            self.assertEqual(len(replans), 1)
            self.assertEqual(replans[0].payload["tasks"][0]["agent_id"], "capital_trace")
            cross_domain = snapshot["final"]["cross_domain"]
            self.assertEqual(cross_domain[0]["symbol"], "600000.SS")
            self.assertIn("capital_behavior", cross_domain[0])

    def test_brain_call_supplements_uncovered_symbols_after_rule_fire(self):
        raw = """生成时间: 2026-07-31 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 9000 4000 1.2 2.5 True
000001.SZ all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(raw)
            repository.save_report(report)
            events = []
            harness = Harness(
                repository,
                events.append,
                PlanCapitalForNearModel(),  # type: ignore[arg-type]
                public_a_stock_client=FakePublicAStock(),  # type: ignore[arg-type]
                global_market_client=FakeGlobalMarket(),  # type: ignore[arg-type]
            )
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=10)

            snapshot = repository.run_snapshot(run_id)
            self.assertEqual(snapshot["status"], "completed")
            capital_messages = [
                event for event in events
                if event.kind == "agent.message" and event.agent_id == "capital_trace"
            ]
            # 规则触发一次（600000.SS ≥ 2×门槛）+ 大脑点名补查一次（000001.SZ 未覆盖）
            self.assertEqual(len(capital_messages), 2)
            stages = {message.payload.get("stage") for message in capital_messages}
            self.assertEqual(stages, {"event_dispatch_result", "brain_dispatch_result"})
            supplement = next(
                message for message in capital_messages
                if message.payload.get("stage") == "brain_dispatch_result"
            )
            per_symbol = supplement.payload["structured_data"]["per_symbol"]
            self.assertEqual([item["symbol"] for item in per_symbol], ["000001.SZ"])

    def test_brain_review_loop_is_capped_by_max_brain_rounds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            model = AlwaysContinueModel()
            harness = Harness(
                repository,
                events.append,
                model,  # type: ignore[arg-type]
                global_market_client=FakeGlobalMarket(),  # type: ignore[arg-type]
            )
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=10)

            snapshot = repository.run_snapshot(run_id)
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(model.review_calls, 2)
            self.assertEqual(
                snapshot["final"]["research_state"]["terminated_reason"], "budget_exhausted"
            )
            replans = [event for event in events if event.kind == "task.replan"]
            self.assertEqual(len(replans), 2)


class EventAgentTests(unittest.TestCase):
    def _run(self, raw, *, tavily=None, public=None, market=None, policy=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = Repository(Path(temp_dir.name) / "test.sqlite")
        report = parse_ptrade_report(raw)
        repository.save_report(report)
        events = []
        harness = Harness(
            repository,
            events.append,
            None,
            None,
            tavily,
            public,
            market or FakeGlobalMarket(),
        )
        run_id = harness.start(report.report_id, policy=policy)
        harness.wait(run_id, timeout=10)
        return repository, events, run_id

    @staticmethod
    def _messages(events, agent_id):
        return [event for event in events if event.kind == "agent.message" and event.agent_id == agent_id]

    def test_event_dispatch_preserves_resident_contributions(self):
        """事件派单只追加不覆盖：最终结果必须包含常驻泳道的全部贡献。"""
        _repository, events, _run_id = self._run(RAW_ANOMALY, public=FakePublicAStock())
        completed = next(event for event in events if event.kind == "run.completed")
        final = completed.payload["final"]
        agent_ids = {item["agent_id"] for item in final["contributions"]}
        self.assertIn("quant_signal", agent_ids)
        self.assertIn("company_industry", agent_ids)
        self.assertIn("global_market", agent_ids)
        self.assertIn("risk", agent_ids)
        self.assertIn("capital_trace", agent_ids)

    def test_capital_trace_fires_on_super_large_anomaly(self):
        repository, events, run_id = self._run(RAW_ANOMALY, public=FakePublicAStock())
        messages = self._messages(events, "capital_trace")
        self.assertEqual(len(messages), 1)
        payload = messages[0].payload
        self.assertEqual(payload["stage"], "event_dispatch_result")
        per = payload["structured_data"]["per_symbol"]
        self.assertEqual(per[0]["symbol"], "600000.SS")
        self.assertEqual(per[0]["classification"], "single_day_pulse")
        dispatch = next(event for event in events if event.kind == "task.event_dispatch")
        self.assertEqual(dispatch.payload["rule_id"], "capital_trace.stock_anomaly")
        self.assertIn("超大单异常=True", dispatch.payload["tasks"][0]["instructions"])
        state = repository.run_snapshot(run_id)["final"]["research_state"]
        self.assertEqual(state["trigger_log"][0]["agent_id"], "capital_trace")
        self.assertTrue(state["trigger_log"][0]["dispatched"])

    def test_capital_trace_fires_on_two_times_threshold(self):
        _repository, events, _run_id = self._run(RAW_OVER_THRESHOLD, public=FakePublicAStock())
        messages = self._messages(events, "capital_trace")
        self.assertEqual(len(messages), 1)
        instructions = next(
            event for event in events if event.kind == "task.event_dispatch"
        ).payload["tasks"][0]["instructions"]
        self.assertIn("资金公式", instructions)
        self.assertIn("2.0×", instructions)

    def test_event_budget_caps_dispatches_per_run(self):
        policy = RunPolicy(max_event_agent_calls=1)
        repository, events, run_id = self._run(
            RAW_ANOMALY, tavily=FakeTavily(), public=FakePublicAStock(), policy=policy
        )
        self.assertEqual(len(self._messages(events, "capital_trace")), 1)
        self.assertEqual(self._messages(events, "bearish_analysis"), [])
        state = repository.run_snapshot(run_id)["final"]["research_state"]
        self.assertEqual(len(state["trigger_log"]), 2)
        blocked = [record for record in state["trigger_log"] if not record["dispatched"]]
        self.assertEqual(len(blocked), 1)
        self.assertIn("未派发", blocked[0]["condition_summary"])

    def test_bearish_analysis_fires_on_risk_category(self):
        _repository, events, _run_id = self._run(RAW, tavily=FakeTavily())
        messages = self._messages(events, "bearish_analysis")
        self.assertEqual(len(messages), 1)
        payload = messages[0].payload
        self.assertIn("监管/合规风险", payload["structured_data"]["trigger_categories"])
        per = payload["structured_data"]["per_symbol"]["600000.SS"]
        self.assertTrue(per["event_driven"])
        self.assertIn("事件性利空", payload["summary"])
        self.assertIn("警示函", payload["summary"])

    def test_global_sector_flow_and_transmission_chain(self):
        market = FakeGlobalMarket(sector_change_percent=3.5, index_change_percent=3.0)
        repository, events, run_id = self._run(RAW, public=FakePublicAStock(), market=market)
        self.assertEqual(len(self._messages(events, "global_sector_flow")), 1)
        transmission_messages = self._messages(events, "sector_transmission")
        self.assertEqual(len(transmission_messages), 1)
        transmissions = transmission_messages[0].payload["structured_data"]["transmissions"]
        self.assertEqual(transmissions[0]["foreign_ticker"], "XLK")
        boards = transmissions[0]["a_share_boards"]
        self.assertTrue(any(board["name"] == "半导体" for board in boards))
        self.assertEqual(boards[0]["direction"], "共振")
        state = repository.run_snapshot(run_id)["final"]["research_state"]
        chain = [record for record in state["trigger_log"] if record["agent_id"] == "sector_transmission"]
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["chained_from"], "global_sector_flow.move")

    def test_demo_sector_snapshot_does_not_dispatch(self):
        market = FakeGlobalMarket(sector_change_percent=3.5, sector_status="demo_fallback")
        _repository, events, _run_id = self._run(RAW, market=market)
        self.assertEqual(self._messages(events, "global_sector_flow"), [])
        self.assertEqual(self._messages(events, "sector_transmission"), [])

    def test_event_agent_fallback_without_data_sources(self):
        _repository, events, _run_id = self._run(RAW_ANOMALY)
        messages = self._messages(events, "capital_trace")
        self.assertEqual(len(messages), 1)
        payload = messages[0].payload
        self.assertIn("资金历史数据不足", payload["summary"])
        self.assertTrue(any("资金历史不可用" in item for item in payload["unknowns"]))
        self.assertEqual(
            payload["structured_data"]["per_symbol"][0]["classification"], "insufficient_data"
        )


if __name__ == "__main__":
    unittest.main()

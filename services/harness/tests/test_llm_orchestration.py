from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quant_agent_harness.harness import Harness
from quant_agent_harness.llm import ModelResult
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository


RAW = """selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""


class FakeModel:
    model = "fake-coordinator"

    def complete_json(self, system: str, user: str) -> ModelResult:
        if "selected_agents" in system:
            return ModelResult(
                data={"selected_agents": ["quant_signal", "global_market"], "rationale": "只需要量化和外围市场。"},
                model=self.model,
            )
        if "title、executive_summary" in system:
            return ModelResult(
                data={
                    "title": "模型综合",
                    "executive_summary": "已综合。",
                    "signal_interpretation": [],
                    "risk_notes": [],
                    "evidence_gaps": ["没有外部证据"],
                },
                model=self.model,
            )
        raise RuntimeError("专业 Agent 故意回退到确定性输出")


class ReplanningModel(FakeModel):
    def __init__(self):
        self.review_calls = 0

    def complete_json(self, system: str, user: str) -> ModelResult:
        if "现在不是做最终总结" in system:
            self.review_calls += 1
            return ModelResult(
                data={
                    "action": "follow_up",
                    "review_summary": "公司资料提到重要监管信息，但缺少进一步核验。",
                    "tasks": [
                        {
                            "agent_id": "company_industry",
                            "instructions": "请核验 600000.SS 是否存在正式监管公告，并说明公告日期和对象。",
                            "symbols": ["600000.SS"],
                            "reason": "重要监管信息需要正式来源支持",
                        }
                    ],
                },
                model=self.model,
            )
        return super().complete_json(system, user)


class LLMOrchestrationTests(unittest.TestCase):
    def test_model_coordinator_cannot_drop_required_agents_and_synthesizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            harness = Harness(repository, events.append, FakeModel())  # type: ignore[arg-type]
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=5)
            plan = next(event for event in events if event.kind == "task.plan")
            self.assertEqual(
                [task["agent_id"] for task in plan.payload["tasks"]],
                ["quant_signal", "company_industry", "global_market"],
            )
            snapshot = repository.run_snapshot(run_id)
            self.assertEqual(snapshot["final"]["title"], "模型综合")
            self.assertEqual(snapshot["final"]["model"], "fake-coordinator")
            silent_pass_stages = {
                event.payload.get("stage")
                for event in events
                if event.kind == "agent.message" and event.agent_id == "coordinator"
            }
            self.assertNotIn("specialist_review", silent_pass_stages)
            self.assertNotIn("post_risk_review", silent_pass_stages)
            self.assertNotIn("synthesis_handoff", silent_pass_stages)

    def test_coordinator_reuses_existing_agent_and_deduplicates_follow_up(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            model = ReplanningModel()
            harness = Harness(repository, events.append, model)  # type: ignore[arg-type]
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=5)

            company_messages = [
                event
                for event in events
                if event.kind == "agent.message" and event.agent_id == "company_industry"
            ]
            self.assertEqual(len(company_messages), 2)
            self.assertTrue(any(event.kind == "task.replan" for event in events))
            coordinator_text = "\n".join(
                str(event.payload.get("content") or "")
                for event in events
                if event.kind == "agent.message" and event.agent_id == "coordinator"
            )
            self.assertIn("追加安排", coordinator_text)
            self.assertIn("核验 600000.SS", coordinator_text)
            self.assertEqual(model.review_calls, 1)
            self.assertFalse(
                any(
                    task.get("agent_id") not in {"quant_signal", "company_industry", "global_market", "risk"}
                    for event in events
                    if event.kind == "task.replan"
                    for task in event.payload.get("tasks") or []
                )
            )


if __name__ == "__main__":
    unittest.main()

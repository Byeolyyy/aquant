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


if __name__ == "__main__":
    unittest.main()

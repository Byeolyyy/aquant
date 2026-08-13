from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quant_agent_harness.harness import Harness
from quant_agent_harness.models import AgentRuntimeConfig
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository
from quant_agent_harness.server import ProtocolServer


RAW = """生成时间: 2026-07-30 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""


class GovernanceObservabilityTests(unittest.TestCase):
    def test_optional_agent_can_be_disabled_and_config_is_versioned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            before = next(item for item in repository.list_agent_configs() if item["agent_id"] == "global_market")
            repository.update_agent_config(
                AgentRuntimeConfig(
                    agent_id="global_market",
                    enabled=False,
                    custom_instructions="只检查最近七天的事件",
                )
            )
            after = next(item for item in repository.list_agent_configs() if item["agent_id"] == "global_market")
            self.assertFalse(after["enabled"])
            self.assertEqual(after["custom_instructions"], "只检查最近七天的事件")
            self.assertGreater(after["config_version"], before["config_version"])

            report = parse_ptrade_report(RAW)
            repository.save_report(report)
            events = []
            harness = Harness(repository, events.append)
            run_id = harness.start(report.report_id)
            harness.wait(run_id, timeout=5)
            plan = next(event for event in events if event.kind == "task.plan")
            self.assertNotIn("global_market", {task["agent_id"] for task in plan.payload["tasks"]})
            self.assertTrue(all(task["config_version"] >= 1 for task in plan.payload["workflow_steps"]))
            self.assertTrue(all("platform.policy:v1+" in task["prompt_version"] for task in plan.payload["workflow_steps"]))

    def test_run_registry_exposes_metrics_and_agent_lifecycle(self):
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
            self.assertGreater(snapshot["metrics"]["event_count"], 0)
            self.assertGreaterEqual(snapshot["metrics"]["agent_count"], 3)
            self.assertGreaterEqual(snapshot["metrics"]["evidence_count"], 1)
            self.assertTrue(any(event.kind == "agent.lifecycle" for event in events))
            registry = repository.list_runs()
            self.assertEqual(registry[0]["run_id"], run_id)
            self.assertEqual(registry[0]["metrics"]["evidence_count"], snapshot["metrics"]["evidence_count"])

    def test_core_governance_agent_cannot_be_disabled_via_protocol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = ProtocolServer(Repository(Path(temp_dir) / "test.sqlite"))
            with self.assertRaisesRegex(ValueError, "不能停用"):
                server.handle(
                    "save_agent_config",
                    {"agent_id": "quant_signal", "enabled": False, "custom_instructions": ""},
                )


if __name__ == "__main__":
    unittest.main()

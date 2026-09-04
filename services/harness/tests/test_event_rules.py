from __future__ import annotations

import unittest

from quant_agent_harness.event_rules import (
    EVENT_RULES,
    SECTOR_TRANSMISSION_MAP,
    EventRuleEngine,
    RuleContext,
)


def _records(context: RuleContext, checkpoint: str = "specialists_done"):
    return EventRuleEngine().evaluate(checkpoint, context)


class EventRuleEngineTests(unittest.TestCase):
    def test_capital_trace_fires_on_super_large_anomaly(self):
        context = RuleContext(
            rows=(
                {"symbol": "600000.SS", "super_large_anomaly": True, "realtime_formula_wanyuan": 100, "effective_threshold": 4000},
            )
        )
        records = _records(context)
        self.assertEqual([record.agent_id for record in records], ["capital_trace"])
        self.assertIn("600000.SS 超大单异常=True", records[0].condition_summary)

    def test_capital_trace_fires_at_two_times_threshold_and_above(self):
        context = RuleContext(
            rows=(
                {"symbol": "600001.SS", "super_large_anomaly": False, "realtime_formula_wanyuan": 8000, "effective_threshold": 4000},
                {"symbol": "600002.SS", "super_large_anomaly": False, "realtime_formula_wanyuan": 9000, "effective_threshold": 4000},
            )
        )
        records = _records(context)
        self.assertEqual(len(records), 1)
        self.assertIn("600001.SS", records[0].condition_summary)
        self.assertIn("600002.SS", records[0].condition_summary)

    def test_capital_trace_does_not_fire_below_threshold(self):
        context = RuleContext(
            rows=(
                {"symbol": "600001.SS", "super_large_anomaly": False, "realtime_formula_wanyuan": 7999, "effective_threshold": 4000},
            )
        )
        self.assertEqual(_records(context), [])

    def test_capital_trace_skips_rows_missing_values(self):
        context = RuleContext(
            rows=(
                {"symbol": "600001.SS", "super_large_anomaly": None, "realtime_formula_wanyuan": None, "effective_threshold": None},
            )
        )
        self.assertEqual(_records(context), [])

    def test_market_move_boundary_and_require_live(self):
        indices = ({"name": "标普500", "change_percent": 2.0},)
        live = _records(RuleContext(market_status="live_delayed", market_indices=indices))
        self.assertEqual([record.agent_id for record in live], ["global_sector_flow"])
        below = _records(RuleContext(market_status="live_delayed", market_indices=({"name": "标普500", "change_percent": 1.99},)))
        self.assertEqual(below, [])
        demo = _records(RuleContext(market_status="demo_fallback", market_indices=indices))
        self.assertEqual(demo, [])

    def test_risk_category_condition(self):
        hit = _records(
            RuleContext(risk_categories=("监管/合规风险", "潜在利空")),
            checkpoint="risk_done",
        )
        self.assertEqual([record.agent_id for record in hit], ["bearish_analysis"])
        miss = _records(RuleContext(risk_categories=("潜在利空",)), checkpoint="risk_done")
        self.assertEqual(miss, [])

    def test_chain_rule_only_fires_after_upstream_and_with_payload(self):
        checkpoint = "after_agent:global_sector_flow"
        context = RuleContext(
            chained_fired=frozenset({"global_sector_flow.move"}),
            chained_structured={
                "global_sector_flow.move": {"anomaly_sectors": [{"name": "XLK 科技", "change_percent": 3.5}]}
            },
        )
        records = _records(context, checkpoint)
        self.assertEqual([record.agent_id for record in records], ["sector_transmission"])
        self.assertEqual(records[0].chained_from, "global_sector_flow.move")
        self.assertIn("XLK 科技 +3.50%", records[0].condition_summary)

        not_fired = _records(
            RuleContext(
                chained_structured={
                    "global_sector_flow.move": {"anomaly_sectors": [{"name": "XLK 科技", "change_percent": 3.5}]}
                }
            ),
            checkpoint,
        )
        self.assertEqual(not_fired, [])

        empty_payload = _records(
            RuleContext(
                chained_fired=frozenset({"global_sector_flow.move"}),
                chained_structured={"global_sector_flow.move": {"anomaly_sectors": []}},
            ),
            checkpoint,
        )
        self.assertEqual(empty_payload, [])

    def test_unknown_checkpoint_fires_nothing(self):
        context = RuleContext(
            rows=({"symbol": "600000.SS", "super_large_anomaly": True, "realtime_formula_wanyuan": 100, "effective_threshold": 4000},),
            risk_categories=("监管/合规风险",),
            market_status="live_delayed",
            market_indices=({"name": "标普500", "change_percent": 3.0},),
        )
        self.assertEqual(_records(context, checkpoint="unknown_phase"), [])

    def test_rules_have_unique_ids_and_known_agents(self):
        ids = [rule.rule_id for rule in EVENT_RULES]
        self.assertEqual(len(ids), len(set(ids)))
        known = {"capital_trace", "bearish_analysis", "global_sector_flow", "sector_transmission"}
        self.assertEqual({rule.agent_id for rule in EVENT_RULES}, known)

    def test_sector_transmission_map_is_complete_and_unique(self):
        self.assertEqual(len(SECTOR_TRANSMISSION_MAP), 10)
        boards: list[str] = []
        for ticker, entry in SECTOR_TRANSMISSION_MAP.items():
            self.assertTrue(entry["name"], f"{ticker} 缺 name")
            self.assertTrue(entry["a_share_boards"], f"{ticker} 缺映射板块")
            boards.extend(entry["a_share_boards"])
        self.assertEqual(len(boards), len(set(boards)), "映射板块关键词不允许重复")


if __name__ == "__main__":
    unittest.main()

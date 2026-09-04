from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from quant_agent_harness.parser import parse_ptrade_report


HEADER = (
    "symbol pct20 turnover_now_pct vol_ratio super_net_wanyuan large_net_wanyuan "
    "medium_net_wanyuan realtime_formula_wanyuan flow_threshold_wanyuan l4_buy_sell "
    "super_large_anomaly reason"
)

# 精简消息：每个区段只打印一次表头，不含 reason、flow_threshold_wanyuan 与回避池。
SLIM_HEADER = (
    "symbol super_net_wanyuan large_net_wanyuan medium_net_wanyuan small_net_wanyuan "
    "realtime_formula_wanyuan realtime_formula_ratio_pct l4_buy_sell vol_ratio turnover_now_pct"
)


class ParserTests(unittest.TestCase):
    def test_valid_report_preserves_false_and_zero(self):
        raw = "\n".join(
            [
                "生成时间: 2026-07-21 14:30:00",
                "运行轮次: 1430",
                "selected_head:",
                HEADER,
                "600000.SS -18 2.5 1.2 3000 1000 0 4300 4000 False False all_conditions_met",
                "near_head: empty",
            ]
        )
        report = parse_ptrade_report(raw)
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.run_slot, "1430")
        self.assertFalse(report.selected_rows[0].l4_buy_sell)
        self.assertEqual(str(report.selected_rows[0].medium_net_wanyuan), "0")

    def test_near_missing_values_is_partial(self):
        report = parse_ptrade_report(
            "selected_head: empty\nnear_head:\n"
            "symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell\n"
            "688258.SS near_miss - - - - -"
        )
        self.assertEqual(report.parse_status, "partial")
        self.assertTrue(report.near_rows[0].missing_fields)

    def test_selected_missing_core_field_is_invalid(self):
        report = parse_ptrade_report(
            "selected_head:\n"
            "symbol reason realtime_formula_wanyuan vol_ratio turnover_now_pct l4_buy_sell\n"
            "600000.SS all_conditions_met 4500 1.2 2 True\n"
            "near_head: empty"
        )
        self.assertEqual(report.parse_status, "invalid")
        self.assertIn("flow_threshold_wanyuan", " ".join(report.parse_errors))

    def test_empty_pools_are_valid(self):
        report = parse_ptrade_report("selected_head: empty\nnear_head: empty")
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.stocks, [])

    def test_empty_pool_on_next_line_is_valid(self):
        # 旧版上游把空池打印成区段名单独一行、下一行 "empty"，
        # 必须同样视为空池而不是缺少数据行。
        report = parse_ptrade_report(
            "selected_head:\nempty\nnear_head:\n"
            + SLIM_HEADER + "\n"
            "600246.SS 4165.07 1000 -800 -600 5143.37 0.195361 False 1.601 6.392079"
        )
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.selected_rows, [])
        self.assertEqual([row.symbol for row in report.near_rows], ["600246.SS"])

    def test_slim_format_with_two_empty_pools_is_valid(self):
        report = parse_ptrade_report("selected_head: empty\nnear_head: empty")
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.stocks, [])

    def test_ptrade_log_format_with_prefixes_is_parsed(self):
        # 用户可能直接复制 PTrade 终端日志：区段标记带时间戳前缀且用 "=" 分隔。
        report = parse_ptrade_report(
            "2026-08-20 13:06:09 - INFO - selected_head=\n"
            + SLIM_HEADER + "\n"
            "600415.SS 8720.21 2674.60 -3891.68 -7503.13 7503.13 0.11 True 1.22 1.09\n"
            "2026-08-20 13:06:09 - INFO - near_head=\n"
            + SLIM_HEADER + "\n"
            "300806.SZ 533.06 879.52 1272.43 -2685.01 2685.01 0.09 False 1.11 2.83"
        )
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual([row.symbol for row in report.selected_rows], ["600415.SS"])
        self.assertEqual([row.symbol for row in report.near_rows], ["300806.SZ"])

    def test_time_like_value_is_not_a_stock(self):
        report = parse_ptrade_report(
            "selected_head:\n" + HEADER + "\n143000 -18 2 1.2 3 2 1 4 4 True False all_conditions_met\nnear_head: empty"
        )
        self.assertEqual(report.parse_status, "invalid")
        self.assertEqual(report.selected_rows, [])

    def test_email_sent_time_in_braces_is_used_as_report_date(self):
        report = parse_ptrade_report(
            "selected_head: empty\n"
            "near_head: empty\n"
            "}邮件发送时间:{2026-07-31 14:31:56}"
        )
        self.assertEqual(report.generated_at, "2026-07-31 14:31:56")
        self.assertEqual(report.report_date, "2026-07-31")

    def test_truncated_tail_and_footer_keep_complete_rows(self):
        # 邮件在最后一个数据行处被截断，尾部残片与页脚粘连，
        # 且 near_head 区段整体丢失：完整行必须保留，缺失区段只是诊断。
        report = parse_ptrade_report(
            "生成时间:{2026-08-17 14:15:00}\n"
            "selected_head:\n" + HEADER + "\n"
            "600000.SS -18 2.5 1.2 3000 1000 0 4300 4000 False False all_conditions_met\n"
            "601133.SS -11.9 2.7 1.4 4000 4300 0 4371 4000 False False all_conditions_met\n"
            "605589.S}邮件发送时间:{2026-08-17 14:16:46}"
        )
        self.assertNotEqual(report.parse_status, "invalid")
        self.assertEqual([row.symbol for row in report.selected_rows], ["600000.SS", "601133.SS"])
        self.assertEqual(report.near_rows, [])
        self.assertEqual(report.report_date, "2026-08-17")
        self.assertTrue(any("缺少 near_head" in item for item in report.diagnostics))
        self.assertFalse(any("无效" in item for item in report.diagnostics))

    def test_footer_glued_to_complete_row_is_dropped_not_misaligned(self):
        # 页脚粘连在完整数据行末尾时，该行整体丢弃，
        # 绝不能把 "14:16:46}" 之类的页脚残片对齐进 reason 等列。
        report = parse_ptrade_report(
            "selected_head:\n" + HEADER + "\n"
            "600000.SS -18 2.5 1.2 3000 1000 0 4300 4000 False False all_conditions_met\n"
            "601133.SS -11.9 2.7 1.4 4000 4300 0 4371 4000 False False all_conditions_met}邮件发送时间:{2026-08-17 14:16:46}\n"
            "near_head: empty"
        )
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual([row.symbol for row in report.selected_rows], ["600000.SS"])
        self.assertTrue(all(row.reason == "all_conditions_met" for row in report.selected_rows))

    def test_footer_only_table_still_fails_closed(self):
        # 整张表都被截断只剩表头和页脚时，没有任何完整行可保留，必须 fail closed。
        report = parse_ptrade_report(
            "selected_head:\n" + HEADER + "\n"
            "邮件发送时间:{2026-08-17 14:16:46}\n"
            "near_head: empty"
        )
        self.assertEqual(report.parse_status, "invalid")
        self.assertEqual(report.selected_rows, [])

    def test_slim_format_injects_threshold_and_reason(self):
        # 精简格式：表头每区段只出现一次，不随行下发 reason 和资金门槛。
        # 解析器必须按区段默认 reason、按策略常量注入 4000 万元门槛，且状态为 valid。
        report = parse_ptrade_report(
            "selected_head:\n" + SLIM_HEADER + "\n"
            "688368.SS 8564.53 3000 -1200 -1500 9421.47 0.334642 True 1.422 1.934845\n"
            "near_head:\n" + SLIM_HEADER + "\n"
            "600246.SS 4165.07 1000 -800 -600 5143.37 0.195361 False 1.601 6.392079\n"
            "}邮件发送时间:{2026-08-18 14:31:46}"
        )
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual(report.generated_at, "2026-08-18 14:31:46")
        selected = report.selected_rows[0]
        self.assertEqual(str(selected.small_net_wanyuan), "-1500")
        self.assertEqual(str(selected.flow_threshold_wanyuan), "4000")
        self.assertEqual(selected.reason, "all_conditions_met")
        self.assertFalse(selected.missing_fields)
        near = report.near_rows[0]
        self.assertEqual(near.reason, "near_miss")
        self.assertEqual(str(near.flow_threshold_wanyuan), "4000")
        self.assertFalse(near.missing_fields)

    def test_slim_format_ignores_avoid_pool_section(self):
        # 旧邮件残留的回避池区段只作为边界被丢弃，不得污染 selected/near 行。
        report = parse_ptrade_report(
            "selected_head:\n" + SLIM_HEADER + "\n"
            "688368.SS 8564.53 3000 -1200 -1500 9421.47 0.334642 True 1.422 1.934845\n"
            "回避池:\n"
            "symbol name\n"
            "000001.SZ 平安银行\n"
            "near_head: empty"
        )
        self.assertEqual(report.parse_status, "valid")
        self.assertEqual([row.symbol for row in report.selected_rows], ["688368.SS"])
        self.assertEqual(report.near_rows, [])

    def test_slim_format_env_overrides_threshold(self):
        # 环境变量可覆盖注入的资金门槛，无需改动上游与消息格式。
        with patch.dict(os.environ, {"PTRADE_FLOW_THRESHOLD_WANYUAN": "5000"}):
            report = parse_ptrade_report(
                "selected_head:\n" + SLIM_HEADER + "\n"
                "688368.SS 8564.53 3000 -1200 -1500 9421.47 0.334642 True 1.422 1.934845\n"
                "near_head: empty"
            )
        self.assertEqual(str(report.selected_rows[0].flow_threshold_wanyuan), "5000")

    def test_slim_large_cap_gets_tiered_threshold_injected(self):
        # 60000 万 ÷ (0.4 × 100) = 1500 亿市值 → 大市值通道门槛 = 1500 × 40 = 60000 万
        report = parse_ptrade_report(
            "selected_head:\n" + SLIM_HEADER + "\n"
            "600519.SS 8564.53 3000 -1200 -1500 60000 0.4 True 1.422 1.934845\n"
            "near_head: empty"
        )
        self.assertEqual(str(report.selected_rows[0].flow_threshold_wanyuan), "60000")

    def test_slim_small_cap_keeps_reference_threshold(self):
        # 5000 万 ÷ (0.5 × 100) = 100 亿市值 → 原门槛（常量 4000 万）
        report = parse_ptrade_report(
            "selected_head:\n" + SLIM_HEADER + "\n"
            "600519.SS 8564.53 3000 -1200 -1500 5000 0.5 True 1.422 1.934845\n"
            "near_head: empty"
        )
        self.assertEqual(str(report.selected_rows[0].flow_threshold_wanyuan), "4000")


class TieredThresholdTests(unittest.TestCase):
    """市值分档门槛（2026-08 新规则）的反推注入。"""

    def test_small_cap_uses_reference_constant(self):
        from quant_agent_harness.parser import tiered_flow_threshold_wanyuan
        from decimal import Decimal
        # 1000 亿以下走原门槛（4000 万参考口径），返回 None 由调用方回退。
        self.assertIsNone(tiered_flow_threshold_wanyuan(Decimal("5000"), Decimal("0.5")))

    def test_large_cap_gets_ratio_based_threshold(self):
        from quant_agent_harness.parser import tiered_flow_threshold_wanyuan
        from decimal import Decimal
        # 60000 万 ÷ (0.4 × 100) = 1500 亿 → 门槛 = 1500 × 40 = 60000 万
        self.assertEqual(
            tiered_flow_threshold_wanyuan(Decimal("60000"), Decimal("0.4")),
            Decimal("60000"),
        )

    def test_boundary_ambiguity_falls_back(self):
        from quant_agent_harness.parser import tiered_flow_threshold_wanyuan
        from decimal import Decimal
        # 4040 万 ÷ (0.04 × 100) = 1010 亿，落在 1000+20 亿模糊窗口内。
        self.assertIsNone(tiered_flow_threshold_wanyuan(Decimal("4040"), Decimal("0.04")))

    def test_missing_or_nonpositive_values_return_none(self):
        from quant_agent_harness.parser import tiered_flow_threshold_wanyuan
        from decimal import Decimal
        self.assertIsNone(tiered_flow_threshold_wanyuan(None, Decimal("0.4")))
        self.assertIsNone(tiered_flow_threshold_wanyuan(Decimal("0"), Decimal("0.4")))
        self.assertIsNone(tiered_flow_threshold_wanyuan(Decimal("60000"), Decimal("0")))

    def test_just_above_boundary_uses_large_cap_threshold(self):
        from quant_agent_harness.parser import tiered_flow_threshold_wanyuan
        from decimal import Decimal
        # 4400 万 ÷ (0.04 × 100) = 1100 亿（窗口外）→ 门槛 1100 × 40 = 44000 万
        self.assertEqual(
            tiered_flow_threshold_wanyuan(Decimal("4400"), Decimal("0.04")),
            Decimal("44000"),
        )


if __name__ == "__main__":
    unittest.main()

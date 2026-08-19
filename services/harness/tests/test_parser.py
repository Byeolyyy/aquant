from __future__ import annotations

import unittest

from quant_agent_harness.parser import parse_ptrade_report


HEADER = (
    "symbol pct20 turnover_now_pct vol_ratio super_net_wanyuan large_net_wanyuan "
    "medium_net_wanyuan realtime_formula_wanyuan flow_threshold_wanyuan l4_buy_sell "
    "super_large_anomaly reason"
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


if __name__ == "__main__":
    unittest.main()

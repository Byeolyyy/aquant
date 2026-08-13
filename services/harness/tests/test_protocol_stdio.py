from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest


class ProtocolStdioTests(unittest.TestCase):
    def test_chinese_report_roundtrips_over_stdio_as_utf8(self):
        report = "\n".join(
            [
                "生成时间: 2026-07-21 14:30:00",
                "运行轮次: 午后测试",
                "selected_head:",
                "symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell",
                "600000.SS 中文原因 4300 4000 1.2 2.5 True",
                "near_head: empty",
            ]
        )
        request = {
            "type": "request",
            "protocol_version": 1,
            "request_id": "chinese-roundtrip",
            "method": "parse_report",
            "payload": {"raw_text": report},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = os.environ.copy()
            environment["QUANT_AGENT_DATA_DIR"] = temp_dir
            completed = subprocess.run(
                [sys.executable, "-m", "quant_agent_harness.server"],
                input=json.dumps(request, ensure_ascii=False) + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                env=environment,
                timeout=15,
                check=True,
            )

        messages = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        response = next(item for item in messages if item.get("type") == "response")
        self.assertTrue(response["ok"])
        parsed = response["result"]["report"]
        self.assertEqual(parsed["raw_text"], report)
        self.assertEqual(parsed["run_slot"], "午后测试")
        self.assertEqual(parsed["selected_rows"][0]["reason"], "中文原因")
        self.assertNotIn("�", completed.stdout)


if __name__ == "__main__":
    unittest.main()

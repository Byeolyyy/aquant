from __future__ import annotations

import unittest
from unittest.mock import patch

from quant_agent_harness.integrations import TavilyClient, TushareClient, normalize_ts_code


class IntegrationClientTests(unittest.TestCase):
    def test_normalizes_ptrade_shanghai_suffix(self):
        self.assertEqual(normalize_ts_code("600000.SS"), "600000.SH")
        self.assertEqual(normalize_ts_code("000001.SZ"), "000001.SZ")

    @patch("quant_agent_harness.integrations._post_json")
    def test_tushare_query_maps_columns_to_rows(self, post_json):
        post_json.return_value = {
            "code": 0,
            "data": {"fields": ["ts_code", "name"], "items": [["600000.SH", "浦发银行"]]},
        }
        rows = TushareClient("token").query(
            "stock_basic", params={"ts_code": "600000.SH"}, fields="ts_code,name"
        )
        self.assertEqual(rows, [{"ts_code": "600000.SH", "name": "浦发银行"}])

    @patch("quant_agent_harness.integrations._post_json")
    def test_tavily_uses_bearer_auth_and_finance_topic(self, post_json):
        post_json.return_value = {"results": []}
        TavilyClient("tvly-secret").search("A股事件", max_results=3)
        _, kwargs = post_json.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tvly-secret")
        payload = post_json.call_args.args[1]
        self.assertEqual(payload["topic"], "finance")
        self.assertEqual(payload["max_results"], 3)


if __name__ == "__main__":
    unittest.main()

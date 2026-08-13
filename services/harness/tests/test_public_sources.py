from __future__ import annotations

import unittest
from unittest.mock import patch

from quant_agent_harness.public_sources import PublicAStockClient


class PublicAStockClientTests(unittest.TestCase):
    def test_research_combines_three_no_key_sources(self):
        client = PublicAStockClient()
        announcement = {"title": "年度报告", "date": "2026-07-20", "url": "https://example.com/a"}
        news = {"title": "公司新闻", "date": "2026-07-19", "url": "https://example.com/n"}
        report = {"title": "机构研报", "date": "2026-07-18", "url": "https://example.com/r"}
        with (
            patch.object(client, "cninfo_announcements", return_value=[announcement]),
            patch.object(client, "eastmoney_news", return_value=[news]),
            patch.object(client, "eastmoney_reports", return_value=[report]),
        ):
            bundles = client.research(["600000.SS"])
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["announcements"], [announcement])
        self.assertEqual(bundles[0]["news"], [news])
        self.assertEqual(bundles[0]["reports"], [report])
        self.assertEqual(bundles[0]["errors"], [])

    def test_research_keeps_partial_results_when_one_source_fails(self):
        client = PublicAStockClient()
        with (
            patch.object(client, "cninfo_announcements", side_effect=RuntimeError("暂时不可用")),
            patch.object(client, "eastmoney_news", return_value=[]),
            patch.object(client, "eastmoney_reports", return_value=[]),
        ):
            bundle = client.research(["000001.SZ"])[0]
        self.assertIn("巨潮公告：暂时不可用", bundle["errors"])


if __name__ == "__main__":
    unittest.main()

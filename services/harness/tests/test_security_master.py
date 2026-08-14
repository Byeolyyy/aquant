from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from quant_agent_harness.harness import Harness
from quant_agent_harness.parser import parse_ptrade_report
from quant_agent_harness.repository import Repository


RAW = """生成时间: 2026-08-14 14:30:00
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty"""


class SecurityMasterTests(unittest.TestCase):
    def test_incremental_import_adds_industry_and_preserves_existing_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            first = repository.upsert_security_master_rows(
                [
                    {"code": "600000", "name": "浦发银行", "industry": "银行"},
                    {"code": "000001", "name": "平安银行", "industry": "银行"},
                ],
                "2026-08-14T16:00:00+08:00",
                source="Table_426.xlsx",
            )
            second = repository.upsert_security_master_rows(
                [{"code": "600000", "name": "XD浦发银", "industry": "股份制银行"}],
                "2026-08-14T16:05:00+08:00",
                source="Table_426.xlsx",
            )

            self.assertEqual(first, {"added": 2, "updated": 0, "unchanged": 0, "skipped": 0})
            self.assertEqual(second["updated"], 1)
            profiles = repository.security_profiles(["600000.SS", "000001.SZ"])
            self.assertEqual(profiles["600000.SS"]["name"], "浦发银行")
            self.assertEqual(profiles["600000.SS"]["industry"], "股份制银行")
            self.assertEqual(profiles["000001.SZ"]["name"], "平安银行")

            repository.sync_security_master(
                [SimpleNamespace(symbol="600000.SS", name="XD浦发银")],
                "2026-08-14T16:10:00+08:00",
            )
            self.assertEqual(
                repository.security_profiles(["600000.SS"])["600000.SS"]["name"],
                "浦发银行",
            )

    def test_existing_database_is_migrated_with_industry_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.sqlite"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE security_master (
                        security_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL UNIQUE,
                        code TEXT NOT NULL,
                        exchange TEXT NOT NULL,
                        current_name TEXT NOT NULL DEFAULT '',
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.commit()
            Repository(database_path)
            with closing(sqlite3.connect(database_path)) as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(security_master)")]
            self.assertIn("industry", columns)

    def test_company_agent_uses_local_master_when_tushare_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            repository.upsert_security_master_rows(
                [{"code": "600000", "name": "浦发银行", "industry": "银行"}],
                "2026-08-14T16:00:00+08:00",
            )
            contribution = Harness(repository)._company_contribution(parse_ptrade_report(RAW))

            self.assertIn("浦发银行", contribution.summary)
            self.assertIn("银行", contribution.summary)
            self.assertTrue(
                any(item.source_type == "local_stable_master" for item in contribution.evidence)
            )

    def test_quant_agent_displays_stable_name_and_registers_master_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            repository.upsert_security_master_rows(
                [{"code": "600000", "name": "浦发银行", "industry": "银行"}],
                "2026-08-14T16:00:00+08:00",
            )
            report = parse_ptrade_report(RAW)
            Harness(repository)._hydrate_report_security_names(report)
            contribution = Harness(repository)._quant_contribution(report)

            self.assertIn("600000.SS｜浦发银行", contribution.summary)
            self.assertTrue(
                any(item.source_type == "local_stable_master" for item in contribution.evidence)
            )

    def test_company_agent_prefers_stable_identity_over_live_basic_name(self):
        class LiveBasic:
            def company_snapshot(self, symbol):
                return {
                    "symbol": symbol,
                    "basic": {"name": "行情临时名", "industry": "临时行业"},
                    "company": {},
                    "daily_basic": {},
                    "financial_indicator": {},
                    "forecast": {},
                    "errors": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            repository.upsert_security_master_rows(
                [{"code": "600000", "name": "浦发银行", "industry": "银行Ⅱ"}],
                "2026-08-14T16:00:00+08:00",
            )
            contribution = Harness(
                repository,
                tushare_client=LiveBasic(),  # type: ignore[arg-type]
            )._company_contribution(parse_ptrade_report(RAW))

            self.assertIn("600000.SS｜浦发银行｜银行Ⅱ", contribution.summary)
            self.assertNotIn("行情临时名", contribution.summary)


if __name__ == "__main__":
    unittest.main()

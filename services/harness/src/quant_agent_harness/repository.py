from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
from uuid import uuid4

from .agent_prompts import PROMPT_DEFINITIONS
from .models import DEFAULT_AGENT_PROFILES, AgentRuntimeConfig, HarnessEvent, ParsedReport
from .secret_store import WindowsDPAPI


def default_data_dir() -> Path:
    configured = os.environ.get("QUANT_AGENT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "QuantAgent"
    return Path.cwd() / ".data"


class Repository:
    def __init__(self, database_path: str | Path | None = None):
        if database_path is None:
            data_dir = default_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            self.database_path = data_dir / "quant-agent.sqlite"
        else:
            self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    parse_status TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reports_hash ON reports(content_hash);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL REFERENCES reports(report_id),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    final_json TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS secrets (
                    secret_key TEXT PRIMARY KEY,
                    encrypted_value BLOB NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS agent_configs (
                    agent_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    custom_instructions TEXT NOT NULL DEFAULT '',
                    config_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    prompt_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    layer TEXT NOT NULL,
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS prompt_versions (
                    version_id TEXT PRIMARY KEY,
                    prompt_id TEXT NOT NULL REFERENCES prompt_templates(prompt_id),
                    version_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    change_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    published_at TEXT,
                    UNIQUE(prompt_id, version_number)
                );
                CREATE INDEX IF NOT EXISTS idx_prompt_versions_prompt
                    ON prompt_versions(prompt_id, version_number DESC);
                CREATE TABLE IF NOT EXISTS security_master (
                    security_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL UNIQUE,
                    code TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    current_name TEXT NOT NULL DEFAULT '',
                    industry TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS security_name_history (
                    security_id TEXT NOT NULL REFERENCES security_master(security_id),
                    name TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT,
                    source TEXT NOT NULL DEFAULT 'ptrade_report',
                    PRIMARY KEY (security_id, name, valid_from)
                );
                CREATE TABLE IF NOT EXISTS signal_observations (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    security_id TEXT NOT NULL REFERENCES security_master(security_id),
                    observed_at TEXT NOT NULL,
                    signal_level TEXT NOT NULL,
                    source_pool TEXT NOT NULL,
                    rank_number INTEGER,
                    formula_wanyuan REAL,
                    threshold_wanyuan REAL,
                    main_net_wanyuan REAL,
                    vol_ratio REAL,
                    turnover_pct REAL,
                    missing_count INTEGER NOT NULL DEFAULT 0,
                    structure_anomaly INTEGER,
                    rule_version TEXT NOT NULL,
                    PRIMARY KEY (run_id, security_id)
                );
                CREATE INDEX IF NOT EXISTS idx_signal_observations_security
                    ON signal_observations(security_id, observed_at DESC);
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    document_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    published_at TEXT NOT NULL DEFAULT '',
                    symbols_json TEXT NOT NULL DEFAULT '[]',
                    retrieved_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    UNIQUE(document_id, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document
                    ON knowledge_chunks(document_id);
                """
            )
            security_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(security_master)").fetchall()
            }
            if "industry" not in security_columns:
                connection.execute(
                    "ALTER TABLE security_master ADD COLUMN industry TEXT NOT NULL DEFAULT ''"
                )
            for profile in DEFAULT_AGENT_PROFILES:
                connection.execute(
                    "INSERT OR IGNORE INTO agent_configs(agent_id, enabled) VALUES (?, ?)",
                    (profile.agent_id, 1 if profile.enabled else 0),
                )
            for definition in PROMPT_DEFINITIONS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO prompt_templates
                        (prompt_id, agent_id, name, description, layer, locked)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        definition["prompt_id"],
                        definition["agent_id"],
                        definition["name"],
                        definition["description"],
                        definition["layer"],
                        1 if definition["locked"] else 0,
                    ),
                )
                connection.execute(
                    """
                    UPDATE prompt_templates
                    SET agent_id=?, name=?, description=?, layer=?, locked=?
                    WHERE prompt_id=?
                    """,
                    (
                        definition["agent_id"],
                        definition["name"],
                        definition["description"],
                        definition["layer"],
                        1 if definition["locked"] else 0,
                        definition["prompt_id"],
                    ),
                )
                exists = connection.execute(
                    "SELECT 1 FROM prompt_versions WHERE prompt_id=? LIMIT 1",
                    (definition["prompt_id"],),
                ).fetchone()
                if not exists:
                    connection.execute(
                        """
                        INSERT INTO prompt_versions
                            (version_id, prompt_id, version_number, content, status, change_note, published_at)
                        VALUES (?, ?, 1, ?, 'published', '系统初始版本', CURRENT_TIMESTAMP)
                        """,
                        (str(uuid4()), definition["prompt_id"], definition["content"]),
                    )
                elif definition.get("template_sections") or definition.get("upgrade_marker"):
                    versions = connection.execute(
                        """
                        SELECT version_id, version_number, content, status, change_note
                        FROM prompt_versions WHERE prompt_id=? ORDER BY version_number
                        """,
                        (definition["prompt_id"],),
                    ).fetchall()
                    published = next((row for row in versions if row["status"] == "published"), None)
                    upgrade_marker = str(
                        definition.get("upgrade_marker")
                        or "【策略配置区：用户可修改】"
                    )
                    is_untouched_legacy = (
                        len(versions) == 1
                        and published is not None
                        and str(published["change_note"]) == "系统初始版本"
                        and upgrade_marker not in str(published["content"])
                    )
                    if is_untouched_legacy:
                        next_version = int(published["version_number"]) + 1
                        connection.execute(
                            "UPDATE prompt_versions SET status='archived' WHERE version_id=?",
                            (published["version_id"],),
                        )
                        connection.execute(
                            """
                            INSERT INTO prompt_versions
                                (version_id, prompt_id, version_number, content, status,
                                 change_note, published_at)
                            VALUES (?, ?, ?, ?, 'published', ?, CURRENT_TIMESTAMP)
                            """,
                            (
                                str(uuid4()),
                                definition["prompt_id"],
                                next_version,
                                definition["content"],
                                str(
                                    definition.get("upgrade_change_note")
                                    or "系统升级：量化策略模板化"
                                ),
                            ),
                        )

    def save_report(self, report: ParsedReport) -> None:
        payload = report.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO reports
                    (report_id, content_hash, parse_status, generated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (report.report_id, report.content_hash, report.parse_status, report.generated_at, payload),
            )

    def get_report(self, report_id: str) -> ParsedReport | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM reports WHERE report_id=?", (report_id,)
            ).fetchone()
        return ParsedReport.model_validate_json(row["payload_json"]) if row else None

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM reports ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        reports = [ParsedReport.model_validate_json(row["payload_json"]) for row in rows]
        return [
            {
                "report_id": report.report_id,
                "content_hash": report.content_hash,
                "generated_at": report.generated_at,
                "report_date": report.report_date,
                "run_slot": report.run_slot,
                "parse_status": report.parse_status,
                "stock_count": len(report.stocks),
            }
            for report in reports
        ]

    def has_history_for(self, report: ParsedReport) -> bool:
        symbols = {row.symbol for row in report.stocks}
        if not symbols:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT report_id, payload_json FROM reports WHERE report_id<>? ORDER BY created_at DESC LIMIT 100",
                (report.report_id,),
            ).fetchall()
        for row in rows:
            prior = ParsedReport.model_validate_json(row["payload_json"])
            if symbols.intersection(item.symbol for item in prior.stocks):
                return True
        return False

    def prior_reports(self, report_id: str, limit: int = 30) -> list[ParsedReport]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM reports WHERE report_id<>? ORDER BY created_at DESC LIMIT ?",
                (report_id, max(1, min(limit, 200))),
            ).fetchall()
        return [ParsedReport.model_validate_json(row["payload_json"]) for row in rows]

    def create_run(self, run_id: str, report_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, report_id, status) VALUES (?, ?, 'planning')",
                (run_id, report_id),
            )

    def update_run(self, run_id: str, status: str, final: dict[str, Any] | None = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE runs SET status=?, updated_at=CURRENT_TIMESTAMP, final_json=? WHERE run_id=?",
                (status, json.dumps(final, ensure_ascii=False, default=str) if final is not None else None, run_id),
            )

    def append_event(self, event: HarnessEvent) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO events(run_id, seq, event_id, event_json) VALUES (?, ?, ?, ?)",
                (event.run_id, event.seq, event.event_id, event.model_dump_json()),
            )

    def run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT run_id, report_id, status, created_at, updated_at, final_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run:
                return None
            events = connection.execute(
                "SELECT event_json FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        event_values = [json.loads(row["event_json"]) for row in events]
        return {
            "run_id": run["run_id"],
            "report_id": run["report_id"],
            "status": run["status"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "final": json.loads(run["final_json"]) if run["final_json"] else None,
            "events": event_values,
            "metrics": _run_metrics(event_values, run["created_at"], run["updated_at"]),
        }

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        with self._connect() as connection:
            runs = connection.execute(
                """
                SELECT r.run_id, r.report_id, r.status, r.created_at, r.updated_at, r.final_json,
                       p.generated_at, p.parse_status, p.payload_json
                FROM runs r JOIN reports p ON p.report_id = r.report_id
                ORDER BY r.created_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            if not runs:
                return []
            placeholders = ",".join("?" for _ in runs)
            event_rows = connection.execute(
                f"SELECT run_id, event_json FROM events WHERE run_id IN ({placeholders}) ORDER BY run_id, seq",
                tuple(row["run_id"] for row in runs),
            ).fetchall()
        events_by_run: dict[str, list[dict[str, Any]]] = {}
        for row in event_rows:
            events_by_run.setdefault(str(row["run_id"]), []).append(json.loads(row["event_json"]))
        values = []
        for row in runs:
            report = ParsedReport.model_validate_json(row["payload_json"])
            final = json.loads(row["final_json"]) if row["final_json"] else None
            values.append(
                {
                    "run_id": row["run_id"],
                    "report_id": row["report_id"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "generated_at": row["generated_at"],
                    "parse_status": row["parse_status"],
                    "symbols": [item.symbol for item in report.stocks],
                    "selected_count": len(report.selected_rows),
                    "near_count": len(report.near_rows),
                    "title": str((final or {}).get("title") or "PTrade 多 Agent 研究"),
                    "summary": str((final or {}).get("executive_summary") or ""),
                    "metrics": _run_metrics(
                        events_by_run.get(str(row["run_id"]), []), row["created_at"], row["updated_at"]
                    ),
                }
            )
        return values

    def list_agent_configs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = {
                str(row["agent_id"]): row
                for row in connection.execute(
                    "SELECT agent_id, enabled, custom_instructions, config_version, updated_at FROM agent_configs"
                ).fetchall()
            }
        values = []
        for profile in DEFAULT_AGENT_PROFILES:
            row = rows.get(profile.agent_id)
            config = AgentRuntimeConfig(
                agent_id=profile.agent_id,
                enabled=bool(row["enabled"]) if row else profile.enabled,
                custom_instructions=str(row["custom_instructions"]) if row else "",
                config_version=int(row["config_version"]) if row else 1,
                updated_at=str(row["updated_at"]) if row else "",
            )
            values.append(
                {
                    **profile.model_dump(mode="json"),
                    **config.model_dump(mode="json"),
                    "required": profile.agent_id in {"coordinator", "quant_signal", "risk"},
                }
            )
        return values

    def update_agent_config(self, config: AgentRuntimeConfig) -> None:
        known = {profile.agent_id for profile in DEFAULT_AGENT_PROFILES}
        if config.agent_id not in known:
            raise ValueError(f"未知 Agent: {config.agent_id}")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_configs(agent_id, enabled, custom_instructions, config_version, updated_at)
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(agent_id) DO UPDATE SET
                    enabled=excluded.enabled,
                    custom_instructions=excluded.custom_instructions,
                    config_version=agent_configs.config_version + 1,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (config.agent_id, 1 if config.enabled else 0, config.custom_instructions),
            )

    def enabled_agent_ids(self) -> set[str]:
        return {item["agent_id"] for item in self.list_agent_configs() if item["enabled"]}

    def agent_custom_instructions(self, agent_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT custom_instructions FROM agent_configs WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return str(row["custom_instructions"]) if row else ""

    def prompt_workspace(self) -> list[dict[str, Any]]:
        active_prompt_ids = [str(item["prompt_id"]) for item in PROMPT_DEFINITIONS]
        definitions = {str(item["prompt_id"]): item for item in PROMPT_DEFINITIONS}
        placeholders = ",".join("?" for _ in active_prompt_ids)
        with self._connect() as connection:
            templates = connection.execute(
                f"""
                SELECT prompt_id, agent_id, name, description, layer, locked, created_at
                FROM prompt_templates
                WHERE prompt_id IN ({placeholders})
                ORDER BY CASE layer WHEN 'platform' THEN 0 ELSE 1 END, agent_id, name
                """,
                active_prompt_ids,
            ).fetchall()
            versions = connection.execute(
                f"""
                SELECT version_id, prompt_id, version_number, content, status, change_note,
                       created_at, COALESCE(published_at, '') AS published_at
                FROM prompt_versions WHERE prompt_id IN ({placeholders})
                ORDER BY prompt_id, version_number DESC
                """,
                active_prompt_ids,
            ).fetchall()
        by_prompt: dict[str, list[dict[str, Any]]] = {}
        for row in versions:
            by_prompt.setdefault(str(row["prompt_id"]), []).append(dict(row))
        return [
            {
                **dict(template),
                "locked": bool(template["locked"]),
                "versions": by_prompt.get(str(template["prompt_id"]), []),
                "template_sections": list(
                    definitions.get(str(template["prompt_id"]), {}).get("template_sections", [])
                ),
                "starter_content": (
                    str(definitions.get(str(template["prompt_id"]), {}).get("content", ""))
                    if definitions.get(str(template["prompt_id"]), {}).get("template_sections")
                    else ""
                ),
            }
            for template in templates
        ]

    def create_prompt_draft(self, prompt_id: str, content: str, change_note: str = "") -> str:
        text = content.strip()
        if len(text) < 40:
            raise ValueError("Prompt 内容过短，至少需要 40 个字符")
        with self._lock, self._connect() as connection:
            template = connection.execute(
                "SELECT locked FROM prompt_templates WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            if not template:
                raise ValueError(f"未知 Prompt: {prompt_id}")
            if bool(template["locked"]):
                raise ValueError("平台策略为只读，不能创建草稿")
            row = connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS value FROM prompt_versions WHERE prompt_id=?",
                (prompt_id,),
            ).fetchone()
            version_number = int(row["value"]) + 1
            version_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO prompt_versions
                    (version_id, prompt_id, version_number, content, status, change_note)
                VALUES (?, ?, ?, ?, 'draft', ?)
                """,
                (version_id, prompt_id, version_number, text, change_note.strip()[:500]),
            )
        return version_id

    def publish_prompt_version(self, version_id: str) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT v.prompt_id, t.locked FROM prompt_versions v
                JOIN prompt_templates t ON t.prompt_id=v.prompt_id WHERE v.version_id=?
                """,
                (version_id,),
            ).fetchone()
            if not row:
                raise ValueError("找不到 Prompt 版本")
            if bool(row["locked"]):
                raise ValueError("平台策略为只读")
            connection.execute(
                "UPDATE prompt_versions SET status='archived' WHERE prompt_id=? AND status='published'",
                (row["prompt_id"],),
            )
            connection.execute(
                "UPDATE prompt_versions SET status='published', published_at=CURRENT_TIMESTAMP WHERE version_id=?",
                (version_id,),
            )

    def rollback_prompt_version(self, prompt_id: str, source_version_id: str) -> str:
        with self._connect() as connection:
            source = connection.execute(
                "SELECT content, version_number FROM prompt_versions WHERE prompt_id=? AND version_id=?",
                (prompt_id, source_version_id),
            ).fetchone()
        if not source:
            raise ValueError("找不到要回滚的 Prompt 版本")
        version_id = self.create_prompt_draft(
            prompt_id,
            str(source["content"]),
            f"回滚自 v{source['version_number']}",
        )
        self.publish_prompt_version(version_id)
        return version_id

    def published_prompt(self, prompt_id: str, fallback: str) -> tuple[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content, version_number FROM prompt_versions
                WHERE prompt_id=? AND status='published'
                ORDER BY version_number DESC LIMIT 1
                """,
                (prompt_id,),
            ).fetchone()
        return (str(row["content"]), int(row["version_number"])) if row else (fallback, 1)

    def sync_security_master(self, stocks: list[Any], observed_at: str) -> None:
        seen_at = observed_at or "unknown"
        with self._lock, self._connect() as connection:
            for stock in stocks:
                symbol = str(stock.symbol).upper()
                code, _, suffix = symbol.partition(".")
                exchange = {"SS": "SH", "SH": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix, suffix)
                security_id = f"CN.{exchange}.{code}"
                name = str(stock.name or "").strip()
                current = connection.execute(
                    "SELECT current_name, industry, aliases_json FROM security_master WHERE security_id=?",
                    (security_id,),
                ).fetchone()
                aliases = set(json.loads(current["aliases_json"])) if current else set()
                if current and current["industry"] and current["current_name"]:
                    name = str(current["current_name"])
                if name and current and current["current_name"] and current["current_name"] != name:
                    aliases.add(str(current["current_name"]))
                    connection.execute(
                        "UPDATE security_name_history SET valid_to=? WHERE security_id=? AND valid_to IS NULL",
                        (seen_at, security_id),
                    )
                connection.execute(
                    """
                    INSERT INTO security_master
                        (security_id, symbol, code, exchange, current_name, aliases_json, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(security_id) DO UPDATE SET
                        symbol=excluded.symbol,
                        current_name=CASE WHEN excluded.current_name<>'' THEN excluded.current_name ELSE security_master.current_name END,
                        aliases_json=excluded.aliases_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (security_id, symbol, code, exchange, name, json.dumps(sorted(aliases), ensure_ascii=False), seen_at, seen_at),
                )
                if name and (not current or current["current_name"] != name):
                    connection.execute(
                        "INSERT OR IGNORE INTO security_name_history(security_id, name, valid_from) VALUES (?, ?, ?)",
                        (security_id, name, seen_at),
                    )

    def upsert_security_master_rows(
        self,
        rows: list[dict[str, Any]],
        observed_at: str,
        *,
        source: str = "manual_import",
    ) -> dict[str, int]:
        """Incrementally add or refresh stable code/name/industry mappings."""
        counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        seen_at = observed_at or "unknown"
        with self._lock, self._connect() as connection:
            for row in rows:
                digits = "".join(character for character in str(row.get("code") or "") if character.isdigit())
                code = digits.zfill(6)[-6:] if digits else ""
                if len(code) != 6:
                    counts["skipped"] += 1
                    continue
                exchange = _exchange_for_code(code)
                if not exchange:
                    counts["skipped"] += 1
                    continue
                symbol = code + (".SS" if exchange == "SH" else f".{exchange}")
                security_id = f"CN.{exchange}.{code}"
                name = str(row.get("name") or "").strip()
                industry = str(row.get("industry") or "").strip()
                current = connection.execute(
                    """
                    SELECT current_name, industry, aliases_json
                    FROM security_master WHERE security_id=?
                    """,
                    (security_id,),
                ).fetchone()
                aliases = set(json.loads(current["aliases_json"])) if current else set()
                if current and _has_temporary_market_prefix(name) and current["current_name"]:
                    name = str(current["current_name"])
                if current and name and current["current_name"] and current["current_name"] != name:
                    aliases.add(str(current["current_name"]))
                    connection.execute(
                        "UPDATE security_name_history SET valid_to=? WHERE security_id=? AND valid_to IS NULL",
                        (seen_at, security_id),
                    )
                if not current:
                    counts["added"] += 1
                elif str(current["current_name"]) != name or str(current["industry"]) != industry:
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
                connection.execute(
                    """
                    INSERT INTO security_master
                        (security_id, symbol, code, exchange, current_name, industry,
                         aliases_json, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(security_id) DO UPDATE SET
                        symbol=excluded.symbol,
                        current_name=CASE WHEN excluded.current_name<>'' THEN excluded.current_name ELSE security_master.current_name END,
                        industry=CASE WHEN excluded.industry<>'' THEN excluded.industry ELSE security_master.industry END,
                        aliases_json=excluded.aliases_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        security_id,
                        symbol,
                        code,
                        exchange,
                        name,
                        industry,
                        json.dumps(sorted(aliases), ensure_ascii=False),
                        seen_at,
                        seen_at,
                    ),
                )
                if name and (not current or current["current_name"] != name):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO security_name_history
                            (security_id, name, valid_from, source)
                        VALUES (?, ?, ?, ?)
                        """,
                        (security_id, name, seen_at, source),
                    )
        return counts

    def security_profiles(self, symbols: list[str]) -> dict[str, dict[str, str]]:
        requested = [str(symbol).upper() for symbol in symbols]
        codes = sorted({symbol.partition(".")[0] for symbol in requested if symbol.partition(".")[0]})
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol, code, exchange, current_name, industry
                FROM security_master WHERE code IN ({placeholders})
                """,
                codes,
            ).fetchall()
        by_code = {
            str(row["code"]): {
                "symbol": str(row["symbol"]),
                "code": str(row["code"]),
                "exchange": str(row["exchange"]),
                "name": str(row["current_name"]),
                "industry": str(row["industry"]),
            }
            for row in rows
        }
        return {
            symbol: by_code[symbol.partition(".")[0]]
            for symbol in requested
            if symbol.partition(".")[0] in by_code
        }

    def record_signal_observations(
        self,
        run_id: str,
        report: ParsedReport,
        levels: dict[str, tuple[str, int | None]],
        *,
        rule_version: str,
    ) -> None:
        observed_at = report.generated_at or report.run_slot or run_id
        with self._lock, self._connect() as connection:
            for stock in report.stocks:
                symbol = stock.symbol.upper()
                code, _, suffix = symbol.partition(".")
                exchange = {"SS": "SH", "SH": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix, suffix)
                security_id = f"CN.{exchange}.{code}"
                level, rank = levels.get(stock.symbol, ("not_selected", None))
                connection.execute(
                    """
                    INSERT OR REPLACE INTO signal_observations
                        (run_id, security_id, observed_at, signal_level, source_pool, rank_number,
                         formula_wanyuan, threshold_wanyuan, main_net_wanyuan, vol_ratio,
                         turnover_pct, missing_count, structure_anomaly, rule_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        security_id,
                        observed_at,
                        level,
                        stock.source_pool,
                        rank,
                        float(stock.realtime_formula_wanyuan) if stock.realtime_formula_wanyuan is not None else None,
                        float(stock.flow_threshold_wanyuan) if stock.flow_threshold_wanyuan is not None else None,
                        float(stock.main_net_wanyuan) if stock.main_net_wanyuan is not None else None,
                        float(stock.vol_ratio) if stock.vol_ratio is not None else None,
                        float(stock.turnover_now_pct) if stock.turnover_now_pct is not None else None,
                        len(stock.missing_fields),
                        None if stock.super_large_anomaly is None else (1 if stock.super_large_anomaly else 0),
                        rule_version,
                    ),
                )

    def signal_stability(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        normalized = symbol.upper()
        code, _, suffix = normalized.partition(".")
        exchange = {"SS": "SH", "SH": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix, suffix)
        security_id = f"CN.{exchange}.{code}"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT signal_level, formula_wanyuan, threshold_wanyuan, missing_count, observed_at
                FROM signal_observations WHERE security_id=?
                ORDER BY rowid DESC LIMIT ?
                """,
                (security_id, max(1, min(limit, 100))),
            ).fetchall()
        levels = [str(row["signal_level"]) for row in rows]
        formula_edges = [
            float(row["formula_wanyuan"]) - float(row["threshold_wanyuan"])
            for row in rows
            if row["formula_wanyuan"] is not None and row["threshold_wanyuan"] is not None
        ]
        return {
            "sample_count": len(rows),
            "formal_count": levels.count("formal"),
            "candidate_count": sum(1 for level in levels if level.startswith("candidate_p")),
            "missing_runs": sum(1 for row in rows if int(row["missing_count"] or 0) > 0),
            "average_funding_edge": round(sum(formula_edges) / len(formula_edges), 2) if formula_edges else None,
            "recent_levels": levels[:5],
        }

    def upsert_knowledge_document(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
    ) -> bool:
        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT document_id FROM knowledge_documents WHERE content_hash=?",
                (document["content_hash"],),
            ).fetchone()
            if exists:
                return False
            connection.execute(
                """
                INSERT INTO knowledge_documents
                    (document_id, content_hash, title, content, url, source_type,
                     published_at, symbols_json, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["document_id"],
                    document["content_hash"],
                    document["title"],
                    document["content"],
                    document.get("url", ""),
                    document["source_type"],
                    document.get("published_at", ""),
                    json.dumps(document.get("symbols", []), ensure_ascii=False),
                    document["retrieved_at"],
                ),
            )
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO knowledge_chunks
                        (chunk_id, document_id, chunk_index, chunk_text, embedding_json, embedding_model)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk["chunk_id"],
                        document["document_id"],
                        chunk["chunk_index"],
                        chunk["chunk_text"],
                        json.dumps(chunk["embedding"], separators=(",", ":")),
                        chunk["embedding_model"],
                    ),
                )
        return True

    def knowledge_candidates(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.document_id, d.title, d.content, d.url, d.source_type,
                       d.published_at, d.symbols_json, d.retrieved_at,
                       c.chunk_id, c.chunk_text, c.embedding_json, c.embedding_model
                FROM knowledge_chunks c
                JOIN knowledge_documents d ON d.document_id=c.document_id
                ORDER BY d.created_at DESC, c.chunk_index ASC LIMIT ?
                """,
                (max(1, min(limit, 5000)),),
            ).fetchall()
        return [
            {
                **dict(row),
                "symbols": json.loads(row["symbols_json"]),
                "embedding": json.loads(row["embedding_json"]),
            }
            for row in rows
        ]

    def knowledge_stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            documents = connection.execute("SELECT COUNT(*) AS value FROM knowledge_documents").fetchone()
            chunks = connection.execute("SELECT COUNT(*) AS value FROM knowledge_chunks").fetchone()
            model = connection.execute(
                "SELECT embedding_model FROM knowledge_chunks ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return {
            "document_count": int(documents["value"]),
            "chunk_count": int(chunks["value"]),
            "embedding_model": str(model["embedding_model"]) if model else "local-hashing-v1",
        }

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT setting_value FROM settings WHERE setting_key=?", (key,)
            ).fetchone()
        return str(row["setting_value"]) if row else default

    def set_settings(self, values: dict[str, str]) -> None:
        with self._lock, self._connect() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO settings(setting_key, setting_value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value=excluded.setting_value,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (key, value),
                )

    def secret_is_configured(self, key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM secrets WHERE secret_key=?", (key,)
            ).fetchone()
        return row is not None

    def get_secret(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_value FROM secrets WHERE secret_key=?", (key,)
            ).fetchone()
        if not row:
            return ""
        return WindowsDPAPI.decrypt(bytes(row["encrypted_value"]))

    def set_secret(self, key: str, plaintext: str) -> None:
        encrypted = WindowsDPAPI.encrypt(plaintext)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO secrets(secret_key, encrypted_value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(secret_key) DO UPDATE SET
                    encrypted_value=excluded.encrypted_value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (key, encrypted),
            )

    def delete_secret(self, key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM secrets WHERE secret_key=?", (key,))


def _exchange_for_code(code: str) -> str:
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    if code.startswith(("4", "8")):
        return "BJ"
    return ""


def _has_temporary_market_prefix(name: str) -> bool:
    normalized = name.strip().upper()
    return normalized.startswith(("XD", "XR", "DR", "N", "C"))


def _run_metrics(events: list[dict[str, Any]], created_at: str, updated_at: str) -> dict[str, Any]:
    evidence_ids: set[str] = set()
    sources: set[str] = set()
    risks = 0
    agents: set[str] = set()
    model_calls = 0
    fallback_count = 0
    error_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    agent_durations: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        agent_id = str(event.get("agent_id") or "")
        payload = event.get("payload") or {}
        if kind == "agent.message" and agent_id:
            agents.add(agent_id)
            for item in payload.get("evidence") or []:
                evidence_id = str(item.get("evidence_id") or "")
                if evidence_id:
                    evidence_ids.add(evidence_id)
                source_type = str(item.get("source_type") or "")
                if source_type:
                    sources.add(source_type)
            risks += len(payload.get("risks") or [])
        elif kind == "model.usage":
            model_calls += 1
            prompt_tokens += int(payload.get("prompt_tokens") or 0)
            completion_tokens += int(payload.get("completion_tokens") or 0)
        elif kind == "model.fallback":
            fallback_count += 1
        elif kind == "run.error":
            error_count += 1
        elif kind == "agent.lifecycle" and payload.get("status") == "completed" and agent_id:
            agent_durations[agent_id] = int(payload.get("duration_ms") or 0)
    try:
        from datetime import datetime

        start = datetime.fromisoformat(str(created_at).replace(" ", "T"))
        end = datetime.fromisoformat(str(updated_at).replace(" ", "T"))
        duration_seconds = max(0, int((end - start).total_seconds()))
    except ValueError:
        duration_seconds = 0
    return {
        "duration_seconds": duration_seconds,
        "event_count": len(events),
        "agent_count": len(agents),
        "evidence_count": len(evidence_ids),
        "source_count": len(sources),
        "risk_count": risks,
        "model_calls": model_calls,
        "fallback_count": fallback_count,
        "error_count": error_count,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "agent_durations_ms": agent_durations,
    }

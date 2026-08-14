from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from quant_agent_harness.repository import Repository


def backup_database(database_path: Path) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_path = database_path.with_name(f"{database_path.stem}.backup-{timestamp}.sqlite")
    with closing(sqlite3.connect(database_path)) as source:
        with closing(sqlite3.connect(backup_path)) as target:
            source.backup(target)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally import stable security names and industries into QuantAgent SQLite."
    )
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--source", default="manual_import")
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input_json).resolve()
    database_path = Path(args.database).resolve()
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Input JSON must contain a list of security rows.")

    backup_path = backup_database(database_path) if args.backup and database_path.exists() else None
    repository = Repository(database_path)
    imported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    counts = repository.upsert_security_master_rows(
        rows,
        imported_at,
        source=args.source,
    )

    with closing(sqlite3.connect(database_path)) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM security_master").fetchone()[0])
        duplicate_codes = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT code FROM security_master GROUP BY code HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        empty_industry = int(
            connection.execute(
                "SELECT COUNT(*) FROM security_master WHERE TRIM(industry) = ''"
            ).fetchone()[0]
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    print(
        json.dumps(
            {
                "database": str(database_path),
                "backup": str(backup_path) if backup_path else "",
                "input_rows": len(rows),
                **counts,
                "total_rows": total,
                "duplicate_codes": duplicate_codes,
                "empty_industry": empty_industry,
                "integrity_check": integrity,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

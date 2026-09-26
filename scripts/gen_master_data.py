"""Load master data CSVs (simulated ERP) into SQLite."""

from __future__ import annotations

import csv
import sqlite3

from rfq_agent.config import MASTER_DIR

TABLES = ["materials", "plants", "work_centers", "outsourced_services", "standard_parts"]


def load_master(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    for t in TABLES:
        with open(MASTER_DIR / f"{t}.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        conn.execute(f"DELETE FROM {t}")
        cols = list(rows[0])
        conn.executemany(
            f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows],
        )
        counts[t] = len(rows)
    conn.commit()
    return counts

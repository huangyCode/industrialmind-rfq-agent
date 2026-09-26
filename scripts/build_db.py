"""Rebuild data/rfq.db from scratch: master data -> historical parts -> costs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gen_master_data import load_master  # noqa: E402

from rfq_agent.config import DB_PATH  # noqa: E402
from rfq_agent.data.db import init_db  # noqa: E402


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = init_db(DB_PATH)
    print("master data:", load_master(conn))
    try:
        from gen_history import build_history
    except ImportError:
        print("history generator not available yet")
        return
    print("history:", build_history(conn))


if __name__ == "__main__":
    main()

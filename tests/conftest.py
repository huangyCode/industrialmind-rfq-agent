import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    from gen_master_data import load_master

    from rfq_agent.data.db import init_db

    path = tmp_path_factory.mktemp("db") / "test.db"
    conn = init_db(path)
    load_master(conn)
    try:
        from gen_history import build_history

        build_history(conn)
    except ImportError:
        pass
    return conn

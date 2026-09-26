"""SQLite schema (simulated ERP / PLM / MES) and connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rfq_agent.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
  code TEXT PRIMARY KEY, name TEXT, standard TEXT, material_no TEXT, mat_group TEXT,
  density_g_cm3 REAL, price_eur_kg REAL, machinability REAL, aliases TEXT
);
CREATE TABLE IF NOT EXISTS plants (
  code TEXT PRIMARY KEY, name TEXT, country TEXT,
  overhead_pct REAL, logistics_pct REAL, base_lead_time_weeks INTEGER
);
CREATE TABLE IF NOT EXISTS work_centers (
  code TEXT, plant TEXT REFERENCES plants(code), name TEXT, rate_eur_h REAL,
  PRIMARY KEY (code, plant)
);
CREATE TABLE IF NOT EXISTS outsourced_services (
  code TEXT PRIMARY KEY, name TEXT, price_eur_kg REAL, lot_min_eur REAL, extra_lead_weeks INTEGER
);
CREATE TABLE IF NOT EXISTS standard_parts (
  part_number TEXT PRIMARY KEY, description TEXT, standard TEXT, match_tokens TEXT,
  unit_price_eur REAL, supplier TEXT, lead_time_days INTEGER
);
CREATE TABLE IF NOT EXISTS parts (
  part_id INTEGER PRIMARY KEY, part_number TEXT UNIQUE, title TEXT, family TEXT,
  shape_class TEXT, material_code TEXT REFERENCES materials(code),
  max_diameter_mm REAL, length_mm REAL, width_mm REAL, height_mm REAL,
  finished_weight_kg REAL, n_holes INTEGER, n_threads INTEGER,
  has_keyway INTEGER, has_gear INTEGER, heat_treatment TEXT, surface_treatment TEXT,
  min_it_grade INTEGER, min_ra_um REAL, spec_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS routings (
  part_id INTEGER REFERENCES parts(part_id), seq INTEGER, op_code TEXT, work_center TEXT,
  setup_min REAL, cycle_min REAL, service_code TEXT,
  PRIMARY KEY (part_id, seq)
);
CREATE TABLE IF NOT EXISTS part_costs (
  part_id INTEGER REFERENCES parts(part_id), plant TEXT, qty INTEGER, unit_cost_eur REAL,
  PRIMARY KEY (part_id, plant, qty)
);
CREATE TABLE IF NOT EXISTS assembly_bom (
  parent_part_id INTEGER REFERENCES parts(part_id), item_no INTEGER, child_part_number TEXT, qty REAL,
  PRIMARY KEY (parent_part_id, item_no)
);
CREATE TABLE IF NOT EXISTS quote_history (
  id INTEGER PRIMARY KEY, part_id INTEGER REFERENCES parts(part_id), customer TEXT, plant TEXT,
  qty INTEGER, unit_price_eur REAL, quoted_at TEXT, won INTEGER
);
CREATE TABLE IF NOT EXISTS quotes (
  quote_id TEXT PRIMARY KEY, rfq_id TEXT, version INTEGER, status TEXT, payload TEXT,
  approved_by TEXT, approved_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, rfq_id TEXT, line_no INTEGER, reviewer TEXT,
  edits TEXT, before TEXT, after TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY, ts TEXT, rfq_id TEXT, node TEXT, provider TEXT, model TEXT,
  prompt_version TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER,
  cache_hit INTEGER, attempts INTEGER, ok INTEGER, error TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str | None = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

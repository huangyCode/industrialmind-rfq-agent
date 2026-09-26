"""Repository layer. Upper layers never write SQL; in production these become SAP / PLM / MES adapters."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property

from rfq_agent.data.db import connect


def norm_token(s: str) -> str:
    """Uppercase, unify multiplication signs, drop whitespace / hyphens / dots."""
    s = s.upper().replace("×", "X").replace("*", "X")
    s = re.sub(r"(?<=\d)\s*X\s*(?=\d|M)", "X", s)
    return re.sub(r"[\s\-\.+/]", "", s)


@dataclass
class Material:
    code: str
    name: str
    standard: str
    material_no: str
    mat_group: str
    density_g_cm3: float
    price_eur_kg: float
    machinability: float
    aliases: list[str]


class Repo:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or connect()

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()


class MaterialRepo(Repo):
    @cached_property
    def all(self) -> dict[str, Material]:
        out = {}
        for r in self.q("SELECT * FROM materials"):
            d = dict(r)
            d["aliases"] = json.loads(d["aliases"] or "[]")
            out[d["code"]] = Material(**d)
        return out

    def get(self, code: str) -> Material:
        return self.all[code]

    def resolve(self, text: str | None) -> str | None:
        """Map a material callout as printed on a drawing to a master-data code."""
        if not text:
            return None
        t = norm_token(text)
        keys: list[tuple[str, str]] = []
        for m in self.all.values():
            for k in [m.code, m.name, m.material_no, *m.aliases]:
                keys.append((norm_token(k), m.code))
        for k, code in keys:
            if t == k:
                return code
        # longest alias contained in the callout, e.g. "42CrMo4+QT 28-32 HRC"
        for k, code in sorted(keys, key=lambda x: -len(x[0])):
            if len(k) >= 4 and k in t:
                return code
        return None


class RateRepo(Repo):
    @cached_property
    def rates(self) -> dict[tuple[str, str], float]:
        return {(r["plant"], r["code"]): r["rate_eur_h"] for r in self.q("SELECT * FROM work_centers")}

    @cached_property
    def plants(self) -> dict[str, dict]:
        return {r["code"]: dict(r) for r in self.q("SELECT * FROM plants")}

    def rate(self, plant: str, work_center: str) -> float | None:
        return self.rates.get((plant, work_center))


class ServiceRepo(Repo):
    @cached_property
    def all(self) -> dict[str, dict]:
        return {r["code"]: dict(r) for r in self.q("SELECT * FROM outsourced_services")}


class CatalogRepo(Repo):
    @cached_property
    def all(self) -> list[dict]:
        out = []
        for r in self.q("SELECT * FROM standard_parts"):
            d = dict(r)
            d["match_tokens"] = json.loads(d["match_tokens"])
            out.append(d)
        return out

    def match(
        self, part_number: str | None, description: str, standard: str | None
    ) -> tuple[dict, str] | None:
        """Return (catalog row, match_method) or None."""
        if part_number:
            pn = norm_token(part_number)
            for row in self.all:
                if norm_token(row["part_number"]) == pn:
                    return row, "exact"
        text = norm_token(" ".join(x for x in (part_number, description, standard) if x))
        for row in self.all:
            if all(norm_token(tok) in text for tok in row["match_tokens"]):
                return row, "normalized"
        return None


class PartRepo(Repo):
    def all(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM parts ORDER BY part_id")]

    def by_number(self, part_number: str) -> dict | None:
        rows = self.q("SELECT * FROM parts WHERE part_number = ?", part_number)
        return dict(rows[0]) if rows else None

    def get(self, part_id: int) -> dict:
        return dict(self.q("SELECT * FROM parts WHERE part_id = ?", part_id)[0])

    def routing(self, part_id: int) -> list[dict]:
        """MES routing; `outsourced` is derived (service_code set or pseudo work centre OUTSOURCED)."""
        rows = [dict(r) for r in self.q("SELECT * FROM routings WHERE part_id = ? ORDER BY seq", part_id)]
        for r in rows:
            r["outsourced"] = bool(r.get("service_code")) or r["work_center"] == "OUTSOURCED"
        return rows

    def spec_json(self, part_id: int) -> str | None:
        """Stored DrawingSpec JSON of a historical part (for leave-one-out backtests); None if absent."""
        rows = self.q("SELECT spec_json FROM parts WHERE part_id = ?", part_id)
        return rows[0]["spec_json"] if rows else None

    def costs(self, part_id: int) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM part_costs WHERE part_id = ?", part_id)]

    def closest_cost(self, part_id: int, qty: int, plant: str | None = None) -> dict | None:
        rows = self.costs(part_id)
        if plant:
            rows = [r for r in rows if r["plant"] == plant] or rows
        if not rows:
            return None
        return min(rows, key=lambda r: (abs(r["qty"] - qty), r["unit_cost_eur"]))

    def quotes(self, part_id: int) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM quote_history WHERE part_id = ?", part_id)]


class QuoteRepo(Repo):
    def save(
        self,
        quote_id: str,
        rfq_id: str,
        version: int,
        status: str,
        payload: str,
        approved_by: str | None,
        approved_at: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?,?)",
            (
                quote_id,
                rfq_id,
                version,
                status,
                payload,
                approved_by,
                approved_at,
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def list(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM quotes ORDER BY created_at DESC")]


class FeedbackRepo(Repo):
    def add(self, rfq_id: str, line_no: int, reviewer: str, edits: str, before: str, after: str) -> None:
        self.conn.execute(
            "INSERT INTO feedback (rfq_id, line_no, reviewer, edits, before, after, created_at) VALUES (?,?,?,?,?,?,?)",
            (rfq_id, line_no, reviewer, edits, before, after, datetime.now().isoformat()),
        )
        self.conn.commit()

    def list(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM feedback ORDER BY id")]


class LLMCallRepo(Repo):
    def add(self, **kw) -> None:
        cols = ",".join(kw)
        self.conn.execute(
            f"INSERT INTO llm_calls ({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values())
        )
        self.conn.commit()

    def list(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM llm_calls ORDER BY id")]

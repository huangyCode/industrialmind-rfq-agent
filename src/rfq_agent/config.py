"""Paths, environment and business parameters."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
MASTER_DIR = DATA_DIR / "master"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
SAMPLES_DIR = DATA_DIR / "samples"
RUNTIME_DIR = DATA_DIR / "runtime"
RENDER_DIR = RUNTIME_DIR / "renders"
FAILURE_DIR = RUNTIME_DIR / "failures"
QUOTE_DIR = RUNTIME_DIR / "quotes"
CHECKPOINT_DB = RUNTIME_DIR / "checkpoints.db"
DB_PATH = Path(os.getenv("RFQ_DB_PATH", DATA_DIR / "rfq.db"))
LLM_CACHE_DIR = ROOT / "fixtures" / "llm_cache"
PROMPT_DIR = Path(__file__).resolve().parent / "llm" / "prompts"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class LLMSettings:
    provider: str = os.getenv("LLM_PROVIDER", "ollama")  # ollama | openai_compat | anthropic
    model: str = os.getenv("LLM_MODEL", "")  # empty = provider default (ollama: gemma4:12b)
    vision_model: str = os.getenv("LLM_VISION_MODEL", "")  # empty = same as model
    mode: str = os.getenv("LLM_MODE", "auto")  # live | record | replay | auto
    cache_dir: str = os.getenv("LLM_CACHE_DIR", "")  # empty = fixtures/llm_cache
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "8192"))
    timeout_s: float = float(os.getenv("LLM_TIMEOUT_S", "600"))
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")

    @classmethod
    def from_env(cls) -> LLMSettings:
        """Re-read the environment (class defaults are frozen at import time)."""
        return cls(
            provider=os.getenv("LLM_PROVIDER", "ollama"),
            model=os.getenv("LLM_MODEL", ""),
            vision_model=os.getenv("LLM_VISION_MODEL", ""),
            mode=os.getenv("LLM_MODE", "auto"),
            cache_dir=os.getenv("LLM_CACHE_DIR", ""),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            timeout_s=float(os.getenv("LLM_TIMEOUT_S", "600")),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "16384")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", ""),
        )


@dataclass(frozen=True)
class BusinessParams:
    margin_pct: float = 0.18
    scrap_pct: float = 0.03
    min_order_value_eur: float = 500.0
    quote_validity_days: int = 30
    fast_track_threshold: float = 0.80
    fast_track_min_similarity: float = 0.85  # FAST_TRACK only with a close precedent part
    manual_threshold: float = 0.55
    similar_min_score: float = 0.50
    blend_min_score: float = 0.60
    similar_top_k: int = 3
    price_deviation_flag: float = 0.30
    outsourced_extra_weeks: int = 1
    standard_bar_diameters: tuple[int, ...] = (
        20,
        25,
        30,
        35,
        40,
        45,
        50,
        55,
        60,
        70,
        80,
        90,
        100,
        110,
        120,
        130,
        140,
        150,
        160,
        180,
        200,
        220,
        250,
    )
    standard_plate_thickness: tuple[int, ...] = (
        5,
        6,
        8,
        10,
        12,
        15,
        20,
        25,
        30,
        35,
        40,
        50,
        60,
        70,
        80,
        90,
        100,
        120,
        150,
    )
    plants: tuple[str, ...] = field(default=("DE", "PL", "CN"))
    assembly_plant: str = "DE"  # plant that builds assemblies and prices their make-parts


LLM = LLMSettings()
BIZ = BusinessParams()

for _d in (RUNTIME_DIR, RENDER_DIR, FAILURE_DIR, QUOTE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

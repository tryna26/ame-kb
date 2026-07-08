"""Runtime configuration loaded from environment (.env)."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    embed_api_key: str
    embed_base_url: str
    embed_model: str
    mysql_dsn: str
    source_dir: str
    graph_no: str
    graph_version: int
    recall_topk: int
    recall_neighbor_topk: int
    min_score_text: float
    min_score_embedding: float


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required env var {name}. Copy .env.example to .env and fill it in."
        )
    return val


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        llm_api_key=_require("LLM_API_KEY"),
        llm_base_url=_require("LLM_BASE_URL"),
        llm_model=_require("LLM_MODEL"),
        embed_api_key=os.getenv("EMBED_API_KEY", ""),
        embed_base_url=os.getenv("EMBED_BASE_URL", ""),
        embed_model=os.getenv("EMBED_MODEL", ""),
        mysql_dsn=_require("MYSQL_DSN"),
        source_dir=os.getenv("SOURCE_DIR", "./data"),
        graph_no=os.getenv("GRAPH_NO", "default"),
        graph_version=int(os.getenv("GRAPH_VERSION", "1")),
        recall_topk=int(os.getenv("RECALL_TOPK", "5")),
        recall_neighbor_topk=int(os.getenv("RECALL_NEIGHBOR_TOPK", "3")),
        min_score_text=float(os.getenv("MIN_SCORE_TEXT", "0")),
        min_score_embedding=float(os.getenv("MIN_SCORE_EMBEDDING", "0")),
    )

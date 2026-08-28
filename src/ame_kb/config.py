"""Runtime configuration loaded from environment (.env)."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator

load_dotenv()


class Settings(BaseModel):
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    embed_api_key: str = ""
    embed_base_url: str = ""
    embed_model: str = ""
    embed_dim: int = 0
    mysql_dsn: str
    source_dir: str
    source_id: Optional[str] = None
    graph_no: str
    graph_version: int
    resolve_low_threshold: float = 0.75
    resolve_high_threshold: float = 0.92
    resolve_candidate_topk: int = 10

    @field_validator("embed_dim")
    @classmethod
    def _non_negative_embed_dim(cls, value: int) -> int:
        # Zero means "not configured" so non-embedding commands remain
        # usable.  embed.py rejects it when an embedding is actually requested.
        if value < 0:
            raise ValueError("EMBED_DIM must not be negative")
        return value

    @field_validator("resolve_candidate_topk")
    @classmethod
    def _positive_candidate_topk(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("RESOLVE_CANDIDATE_TOPK must be a positive integer")
        return value

    @model_validator(mode="after")
    def _valid_resolve_thresholds(self) -> "Settings":
        low = self.resolve_low_threshold
        high = self.resolve_high_threshold
        if not 0 <= low < high <= 1:
            raise ValueError(
                "resolve thresholds must satisfy "
                "0 <= RESOLVE_LOW_THRESHOLD < RESOLVE_HIGH_THRESHOLD <= 1"
            )
        return self


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required env var {name}. Copy .env.example to .env and fill it in."
        )
    return val


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    llm_api_key = _require("LLM_API_KEY")
    llm_base_url = _require("LLM_BASE_URL")
    return Settings(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=_require("LLM_MODEL"),
        # Credentials/endpoint may share the chat gateway.  The embedding model
        # deliberately cannot fall back to LLM_MODEL: they are different API
        # contracts even when served by the same OpenAI-compatible endpoint.
        embed_api_key=os.getenv("EMBED_API_KEY") or llm_api_key,
        embed_base_url=os.getenv("EMBED_BASE_URL") or llm_base_url,
        embed_model=os.getenv("EMBED_MODEL", ""),
        embed_dim=int(os.getenv("EMBED_DIM", "0")),
        mysql_dsn=_require("MYSQL_DSN"),
        source_dir=os.getenv("SOURCE_DIR", "./data"),
        source_id=os.getenv("SOURCE_ID") or None,
        graph_no=os.getenv("GRAPH_NO", "default"),
        graph_version=int(os.getenv("GRAPH_VERSION", "1")),
        resolve_low_threshold=float(os.getenv("RESOLVE_LOW_THRESHOLD", "0.75")),
        resolve_high_threshold=float(
            os.getenv("RESOLVE_HIGH_THRESHOLD", "0.92")
        ),
        resolve_candidate_topk=int(os.getenv("RESOLVE_CANDIDATE_TOPK", "10")),
    )

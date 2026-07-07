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
    mysql_dsn: str
    source_dir: str
    graph_no: str
    graph_version: int


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
        mysql_dsn=_require("MYSQL_DSN"),
        source_dir=os.getenv("SOURCE_DIR", "./data"),
        graph_no=os.getenv("GRAPH_NO", "default"),
        graph_version=int(os.getenv("GRAPH_VERSION", "1")),
    )

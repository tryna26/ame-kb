"""Select the HybridIndex backend from SEARCH_BACKEND (mysql|redis)."""
from __future__ import annotations

from functools import lru_cache

from .base import HybridIndex


@lru_cache(maxsize=1)
def get_index() -> HybridIndex:
    from ..config import get_settings

    backend = get_settings().search_backend.lower()
    if backend == "mysql":
        from .mysql import MysqlHybridIndex

        return MysqlHybridIndex()
    if backend == "redis":
        from .redis import RedisHybridIndex

        return RedisHybridIndex()
    raise ValueError(
        f"Unknown SEARCH_BACKEND '{backend}'. Use 'mysql' or 'redis'."
    )

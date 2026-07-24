"""In-process explored-state store for stateful, progressive graph exploration.

Ported from general_recall graph_explorer.go's per-state_id explored cache
(sync.Map[stateID] -> set[entityID]). An agent that keeps passing the same
``state_id`` across successive ``recall`` calls gets *new* nodes each time: the
nodes already returned under that state are excluded from seeds and neighbor
expansion, so "dig deeper / what else?" follow-ups never repeat results.

Storage is deliberately process-local (option 1): a plain dict guarded by a
lock, with lazy TTL eviction. This adds zero new dependencies and suits the
data (small, per-session, disposable). The trade-off is that state is not shared
across worker processes/instances and is lost on restart -- acceptable for a
single-process server; switch to Redis/MySQL if you later run multiple
instances.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Iterable, List, Set, Tuple

from .config import get_settings


class _StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # state_id -> (explored node_nos, last_access_epoch)
        self._states: Dict[str, Tuple[Set[str], float]] = {}

    def _evict_expired_locked(self, now: float, ttl: float) -> None:
        stale = [
            sid
            for sid, (_nos, last) in self._states.items()
            if now - last > ttl
        ]
        for sid in stale:
            del self._states[sid]

    def _enforce_capacity_locked(self, max_states: int) -> None:
        # Drop least-recently-accessed states beyond the cap.
        if max_states <= 0 or len(self._states) <= max_states:
            return
        ordered = sorted(self._states.items(), key=lambda kv: kv[1][1])
        for sid, _ in ordered[: len(self._states) - max_states]:
            del self._states[sid]

    def get(self, state_id: str) -> Set[str]:
        """Return a copy of the explored set for ``state_id`` (empty if unknown)."""
        settings = get_settings()
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now, settings.recall_state_ttl_seconds)
            entry = self._states.get(state_id)
            if entry is None:
                return set()
            explored, _last = entry
            self._states[state_id] = (explored, now)  # touch (LRU)
            return set(explored)

    def add(self, state_id: str, node_nos: Iterable[str]) -> int:
        """Mark ``node_nos`` explored under ``state_id``. Returns the new total."""
        settings = get_settings()
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now, settings.recall_state_ttl_seconds)
            explored, _last = self._states.get(state_id, (set(), now))
            explored = set(explored)
            explored.update(n for n in node_nos if n)
            self._states[state_id] = (explored, now)
            self._enforce_capacity_locked(settings.recall_state_max_states)
            return len(explored)

    def clear(self, state_id: str) -> bool:
        """Forget one state. Returns True if it existed."""
        with self._lock:
            return self._states.pop(state_id, None) is not None

    def reset(self) -> None:
        """Drop all states (used by tests)."""
        with self._lock:
            self._states.clear()

    def active_states(self) -> List[str]:
        with self._lock:
            return list(self._states.keys())


_STORE = _StateStore()


def state_key(graph_no: str, graph_version: int, state_id: str) -> str:
    """Namespace a caller-supplied ``state_id`` by the active graph version.

    node_nos are only unique within a (graph_no, graph_version); the same
    ``state_id`` reused against a different graph must not cross-exclude nodes,
    so the store key carries the graph scope.
    """
    return f"{graph_no}:{graph_version}:{state_id}"


def get_explored(state_id: str) -> Set[str]:
    return _STORE.get(state_id)


def mark_explored(state_id: str, node_nos: Iterable[str]) -> int:
    return _STORE.add(state_id, node_nos)


def clear_state(state_id: str) -> bool:
    return _STORE.clear(state_id)


def active_states() -> List[str]:
    return _STORE.active_states()


def reset_all() -> None:
    _STORE.reset()

"""检索结果的进程内 TTL 缓存。

同一问题在短时间内重复检索（多轮对话、评测重放）直接命中缓存；
任何文档写入/删除/重建索引都会清空缓存。键只含影响结果的参数，
不含内部生成的 embedding（调用方显式传入 embedding 时绕过缓存）。
"""

import hashlib
import json
import threading
import time
from copy import deepcopy
from typing import Any

_lock = threading.Lock()
_entries: dict[str, tuple[float, Any]] = {}


def _max_entries() -> int:
    from app.core.config import settings

    return settings.rag_retrieval_cache_max_entries


def _ttl() -> float:
    from app.core.config import settings

    return float(settings.rag_retrieval_cache_ttl_seconds)


def make_cache_key(**params: Any) -> str | None:
    """根据影响检索结果的参数生成缓存键；TTL 关闭时返回 None。"""
    if _ttl() <= 0:
        return None
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_get(key: str) -> list[dict] | None:
    if key is None:
        return None
    now = time.monotonic()
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if now - stored_at > _ttl():
            _entries.pop(key, None)
            return None
        # 深拷贝避免调用方修改缓存内容。
        return deepcopy(value)


def cache_put(key: str, value: list[dict]) -> None:
    if key is None or _ttl() <= 0:
        return
    now = time.monotonic()
    with _lock:
        if key in _entries:
            _entries[key] = (now, deepcopy(value))
            return
        # 简单淘汰：超过容量时清掉最早过期的一半，保持实现无依赖。
        while len(_entries) >= _max_entries():
            oldest = sorted(_entries.items(), key=lambda item: item[1][0])[: max(1, len(_entries) // 2)]
            for stale_key, _ in oldest:
                _entries.pop(stale_key, None)
        _entries[key] = (now, deepcopy(value))


def clear_retrieval_cache() -> None:
    """索引内容变化时调用：清空全部检索缓存。"""
    with _lock:
        _entries.clear()

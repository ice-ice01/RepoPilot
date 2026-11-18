"""LLM 消耗型接口的每用户滑动窗口限流。

聊天与 RAGAS 评测会真实调用付费模型，任何登录用户都不应无节制触发。
实现是进程内内存窗口，适合单实例部署；0 关闭限流。
"""

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Depends, HTTPException

from app.core.config import settings
from app.db.models import User
from app.services.auth import get_current_user

_lock = threading.Lock()
_recent_requests: dict[str, deque[float]] = {}


def _hit_quota(key: str, limit_per_minute: int) -> bool:
    now = time.monotonic()
    with _lock:
        window = _recent_requests.setdefault(key, deque())
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= limit_per_minute:
            return False
        window.append(now)
        return True


def require_llm_rate_limit() -> Callable:
    """作为路由依赖使用：超过每分钟配额时返回 429。"""

    def dependency(user: User = Depends(get_current_user)) -> User:
        limit = settings.llm_request_rate_limit_per_minute
        if limit > 0 and not _hit_quota(f"llm:{user.id}", limit):
            raise HTTPException(status_code=429, detail=f"请求过于频繁，每分钟最多 {limit} 次 LLM 调用，请稍后再试。")
        return user

    return dependency

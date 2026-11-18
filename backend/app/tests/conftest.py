"""测试全局夹具与性能开关。

- PBKDF2 迭代数降到 2000：登录类测试不再被 600k 次哈希拖慢（生产默认不受影响）。
- 每个用例前后重置模块级共享 HTTP 连接池与检索缓存：测试常用 MockTransport
  替换 httpx.AsyncClient 构造器，共享池若跨用例复用会拿到别的用例的（甚至真实的）传输层。
"""

import os

os.environ.setdefault("PBKDF2_ITERATIONS", "2000")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_level_state():
    from app.services.github import client as github_client_module
    from app.services.llm import client as llm_client_module
    from app.services.rag.retrieval_cache import clear_retrieval_cache

    def _reset() -> None:
        llm_client_module._shared_client = None
        github_client_module._shared_client = None
        clear_retrieval_cache()

    _reset()
    yield
    _reset()

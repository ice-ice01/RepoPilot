from collections.abc import Iterable
from typing import Literal


RetrievalMode = Literal["rag", "direct"]

# Java / Spring AI 对照：这里像决定“哪些业务数据可以交给 VectorStore”的策略类。
# 白名单表示允许向量化，不代表已写入 Milvus；还要看写入入口是否执行 embedding / 同步。
# Document 表也存 direct 资料，所以“数据库里有 Document”不等于“RAG 能检索到”。

# RAG is reserved for reusable, unstructured project knowledge where semantic
# recall is useful. Structured live facts remain in their source tables/APIs.
RAG_SOURCE_TYPES = frozenset(
    {
        "issue",
        "pull_request",
        "workflow_run",
        "project_doc",
        "knowledge_file",
        "memory_note",
    }
)

DIRECT_SOURCE_TYPES = frozenset(
    {
        "project_overview",
        "project_manifest",
        "team_member",
        "weekly_report",
        "chat_session",
        "conversation_memory",
        "thread_memory",
    }
)


def retrieval_mode(source_type: str) -> RetrievalMode:
    # code_file 不在白名单，vector_store 还会拒绝保存源码；源码走工作区实时搜索。
    # 对话、团队、周报、项目概况/依赖清单等，走业务表、记忆或工作区工具直接查询。
    if source_type in RAG_SOURCE_TYPES:
        return "rag"
    return "direct"


def normalize_source_types(source_types: Iterable[str | None] | None) -> list[str]:
    normalized = [str(source_type).strip() for source_type in (source_types or []) if source_type and str(source_type).strip()]
    return list(dict.fromkeys(normalized))


def should_vectorize(source_type: str) -> bool:
    return retrieval_mode(source_type) == "rag"


def is_searchable_source(source_type: str) -> bool:
    return retrieval_mode(source_type) == "rag"


def policy_metadata(source_type: str) -> dict[str, object]:
    mode = retrieval_mode(source_type)
    return {
        "retrieval_mode": mode,
        "rag_enabled": mode == "rag",
    }

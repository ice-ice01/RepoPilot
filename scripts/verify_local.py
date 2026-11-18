"""Verify the running full environment using temporary, isolated application data."""

import getpass
import os
import time
import uuid

import httpx

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal


def verify() -> None:
    assert settings.database_url.startswith("postgresql+")
    assert settings.milvus_enabled
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=240, trust_env=False) as client:
        username = os.environ.get("DEVFLOW_VERIFY_USERNAME")
        if not username:
            raise RuntimeError("Set DEVFLOW_VERIFY_USERNAME to an existing owner account first")
        password = os.environ.get("DEVFLOW_VERIFY_PASSWORD") or getpass.getpass("Verification account password: ")
        login = client.post("/api/auth/login", json={"username": username, "password": password})
        login.raise_for_status()
        user = login.json()
        if user["role"] != "owner":
            client.headers["X-CSRF-Token"] = client.cookies["devflow_csrf"]
            client.post("/api/auth/logout").raise_for_status()
            raise RuntimeError("Verification requires owner permissions for cleanup")
        client.headers["X-CSRF-Token"] = client.cookies["devflow_csrf"]
        repo_id = uuid.uuid4()
        with SessionLocal() as db:
            db.add(Repository(
                id=repo_id, owner_user_id=uuid.UUID(user["id"]), owner="local-validation", name=str(repo_id),
                full_name=f"local-validation/{repo_id}", provider="github",
                description="Temporary startup validation; automatically removed.",
            ))
            db.commit()
        print("PostgreSQL: temporary validation namespace created", flush=True)
        try:
            client.get("/health").raise_for_status()
            config = client.patch(f"/api/rag/repositories/{repo_id}/config", json={
                "retrieval_method": "hybrid", "score_threshold_enabled": False,
                "rerank_provider": "heuristic", "rerank_model": "heuristic-reranker",
            })
            config.raise_for_status()
            upload = client.post(f"/api/rag/repositories/{repo_id}/documents", files={
                "file": ("startup-validation.txt", b"The DevFlow startup validation code is VECTOR-1024. Local Qwen creates 1024-dimensional vectors stored in Milvus.", "text/plain"),
            })
            upload.raise_for_status()
            document_id = upload.json()["id"]
            for _ in range(90):
                response = client.get(f"/api/rag/repositories/{repo_id}/documents")
                response.raise_for_status()
                document = next(item for item in response.json() if item["id"] == document_id)
                if document["status"] == "completed":
                    break
                if document["status"] == "failed":
                    raise RuntimeError("Validation document processing failed")
                time.sleep(1)
            else:
                raise RuntimeError("Validation document processing timed out")
            print(f"Upload + local Qwen indexing: {document['chunk_count']} chunks", flush=True)
            query = {"question": "What is the DevFlow startup validation code?", "top_k": 3}
            search = client.post(f"/api/rag/{repo_id}/search", json=query)
            search.raise_for_status()
            data = search.json()
            sources = data.get("results", [])
            assert sources and any("VECTOR-1024" in source["snippet"] for source in sources)
            assert any("vector" in (source.get("retrieval") or {}).get("sources", []) for source in sources)
            print("Milvus: real Qwen vector recall passed", flush=True)
            answer = client.post(f"/api/rag/{repo_id}/ask", json=query)
            answer.raise_for_status()
            result = answer.json()
            assert "VECTOR-1024" in result["answer"]
            assert result["sources"]
            assert result["generation_mode"] == "llm"
            assert result["model"] == settings.llm_model
            print("RAG answer + citations: passed", flush=True)
            chat = client.post("/api/chat", json={
                "repo_id": str(repo_id), "message": "Reply with OK only. Do not call tools.",
            })
            chat.raise_for_status()
            result = chat.json()
            assert result["answer"] and result.get("model_usage")
            print("ChatAgent: live configured AI model passed", flush=True)
        finally:
            try:
                cleanup = client.delete(f"/api/repos/{repo_id}")
                cleanup.raise_for_status()
                assert cleanup.json()["deleted"]
                print("Temporary validation namespace and uploaded file removed", flush=True)
            finally:
                client.post("/api/auth/logout").raise_for_status()


if __name__ == "__main__":
    verify()

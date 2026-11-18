"""HTTP authentication and multi-user isolation regressions (no external services)."""

from datetime import timedelta
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.orm import aliased, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db import models as m
from app.db.auth_migration import migrate_auth_schema
from app.db.session import get_db
from app.main import create_app
from app.services.auth import CSRF_COOKIE, SESSION_COOKIE, hash_password, token_hash, utcnow, verify_password
from app.services.data_access import bind_user

PASSWORD = "test-password-12345"


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_allow_registration", False)
    monkeypatch.setattr(settings, "auth_bootstrap_token", None)
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    m.Base.metadata.create_all(engine)
    migrate_auth_schema(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app()

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    yield app, factory
    engine.dispose()


def make_client(app, host="127.0.0.1", base_url="http://localhost"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 12345)), base_url=base_url)


@pytest_asyncio.fixture
async def client(auth_env):
    async with make_client(auth_env[0]) as result:
        yield result


def csrf(client):
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE, "")}


async def setup(client):
    response = await client.post("/api/auth/register", json={"username": "Admin", "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


async def add_user(client, username, role="owner"):
    response = await client.post("/api/auth/users", headers=csrf(client),
                                 json={"username": username, "password": PASSWORD, "role": role})
    assert response.status_code == 201, response.text
    return response.json()


def seed_user_data(factory, user_id, repo_name="shared"):
    with factory() as db:
        repo = m.Repository(owner_user_id=UUID(user_id), owner="example", name=repo_name, full_name=f"example/{repo_name}")
        db.add(repo)
        db.flush()
        conversation = m.Conversation(repo_id=repo.id, title="private conversation")
        issue = m.Issue(repo_id=repo.id, number=1, title="private issue")
        pr = m.PullRequest(repo_id=repo.id, number=1, title="private PR")
        run = m.WorkflowRun(repo_id=repo.id, name="private CI", status="completed")
        doc = m.KnowledgeSourceDocument(repo_id=repo.id, name="secret.txt", stored_path="private.txt", checksum="a" * 64)
        draft = m.ActionDraft(repo_id=repo.id, draft_type="send_report", target_type="repository", content="private", created_by=UUID(user_id))
        workspace = m.Workspace(created_by=UUID(user_id), name="private workspace", repo_ids=[str(repo.id)])
        evaluation = m.EvalRun(owner_user_id=UUID(user_id), name="private eval")
        analysis = m.AnalysisResult(owner_user_id=UUID(user_id), target_type="issue", target_id=uuid4(), analysis_type="issue")
        audit = m.AuditLog(repo_id=repo.id, user_id=UUID(user_id), action="private")
        workflow = m.AgentWorkflowRun(repo_id=repo.id, goal="private task")
        db.add_all([conversation, issue, pr, run, doc, draft, workspace, evaluation, analysis, audit, workflow])
        db.flush()
        message = m.ChatMessage(repo_id=repo.id, conversation_id=conversation.id, role="assistant", content="secret")
        trace = m.AgentRun(repo_id=repo.id, conversation_id=conversation.id, user_message="secret", route="chat", final_answer="secret")
        task = m.AgentTaskRun(workflow_id=workflow.id, task_id="t1", agent_name="issue", task_type="analyze")
        pr_file = m.PRFile(pr_id=pr.id, filename="secret.py", status="modified")
        comment = m.PRReviewComment(pr_id=pr.id, body="secret")
        db.add_all([message, trace, task, pr_file, comment])
        db.commit()
        return {name: str(row.id) for name, row in locals().copy().items() if isinstance(row, m.Base)}


@pytest.mark.asyncio
@pytest.mark.parametrize("demo_mode", [True, False])
async def test_all_business_routes_require_auth(client, auth_env, monkeypatch, demo_mode):
    monkeypatch.setattr(settings, "demo_mode", demo_mode)
    checked = 0
    for route in auth_env[0].routes:
        if not route.path.startswith("/api/") or route.path.startswith(("/api/auth/", "/api/webhooks/")):
            continue
        path = route.path
        for parameter in route.param_convertors:
            path = path.replace("{" + parameter + "}", str(uuid4()))
        for method in route.methods - {"OPTIONS", "HEAD"}:
            response = await client.request(method, path, json={})
            assert response.status_code == 401, (method, path, response.text)
            checked += 1
    assert checked > 80
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/auth/status")).status_code == 200


@pytest.mark.asyncio
async def test_setup_claims_legacy_data_and_hashes_secrets(client, auth_env):
    with auth_env[1]() as db:
        legacy = m.User(name="demo")
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
        repo = m.Repository(owner="old", name="repo", full_name="old/repo")
        db.add(repo)
        db.commit()
    admin = await setup(client)
    assert admin["id"] == str(legacy_id)
    assert admin["username"] == "admin" and admin["is_admin"]
    assert set(admin) == {"id", "username", "name", "role", "is_admin"}
    token = client.cookies[SESSION_COOKIE]
    with auth_env[1]() as db:
        user = db.get(m.User, legacy_id)
        assert user.password_hash != PASSWORD and verify_password(PASSWORD, user.password_hash)
        session = db.query(m.AuthSession).one()
        assert session.token_hash == token_hash(token) and session.token_hash != token
        assert session.csrf_hash == token_hash(client.cookies[CSRF_COOKIE])
        assert db.query(m.Repository).one().owner_user_id == legacy_id
    assert len((await client.get("/api/repos")).json()) == 1
    assert (await client.post("/api/auth/register", json={"username": "intruder", "password": PASSWORD})).status_code == 403


@pytest.mark.asyncio
async def test_bootstrap_requires_local_or_secret(auth_env, monkeypatch):
    async with make_client(auth_env[0], "203.0.113.8") as remote:
        assert (await remote.post("/api/auth/register", json={"username": "owner", "password": PASSWORD})).status_code == 403
        monkeypatch.setattr(settings, "auth_bootstrap_token", "setup-secret")
        assert (await remote.post("/api/auth/register", json={"username": "owner", "password": PASSWORD})).status_code == 403
        response = await remote.post("/api/auth/register", headers={"X-Bootstrap-Token": "setup-secret"},
                                     json={"username": "owner", "password": PASSWORD})
        assert response.status_code == 201
    with auth_env[1]() as db:
        assert db.query(m.User).count() == 1


@pytest.mark.asyncio
async def test_production_cookie_flags_and_bootstrap(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    assert (await client.post("/api/auth/register", json={"username": "owner", "password": PASSWORD})).status_code == 403
    monkeypatch.setattr(settings, "auth_bootstrap_token", "secret")
    response = await client.post("/api/auth/register", headers={"X-Bootstrap-Token": "secret"}, json={"username": "owner", "password": PASSWORD})
    cookies = response.headers.get_list("set-cookie")
    assert response.status_code == 201
    assert all("Secure" in cookie and "SameSite=lax" in cookie for cookie in cookies)
    assert "HttpOnly" in next(cookie for cookie in cookies if cookie.startswith(SESSION_COOKIE + "="))
    assert "HttpOnly" not in next(cookie for cookie in cookies if cookie.startswith(CSRF_COOKIE + "="))


@pytest.mark.asyncio
async def test_csrf_origin_logout_and_bearer(client):
    await setup(client)
    token = client.cookies[SESSION_COOKIE]
    assert (await client.post("/api/auth/logout")).status_code == 403
    assert (await client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"})).status_code == 403
    assert (await client.post("/api/auth/logout", headers={**csrf(client), "Origin": "https://evil.example"})).status_code == 403
    assert (await client.get("/api/auth/me", headers={"Authorization": "Bearer bad"})).status_code == 401
    assert (await client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 200
    assert (await client.post("/api/auth/logout", headers=csrf(client))).status_code == 200
    assert SESSION_COOKIE not in client.cookies
    assert (await client.get("/api/repos", headers={"Authorization": "Bearer " + token})).status_code == 401


@pytest.mark.asyncio
async def test_password_change_revokes_other_sessions_and_expiry(client, auth_env):
    admin = await setup(client)
    old_token = client.cookies[SESSION_COOKIE]
    assert (await client.post("/api/auth/login", json={"username": "ADMIN", "password": PASSWORD})).status_code == 200
    response = await client.post("/api/auth/password", headers=csrf(client),
                                 json={"current_password": PASSWORD, "new_password": "changed-password-123"})
    assert response.status_code == 200
    assert (await client.get("/api/repos", headers={"Authorization": "Bearer " + old_token})).status_code == 401
    with auth_env[1]() as db:
        assert db.query(m.AuthSession).count() == 1
        session = db.query(m.AuthSession).one()
        session.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert (await client.get("/api/repos")).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "admin", "password": "changed-password-123"})).status_code == 200
    with auth_env[1]() as db:
        db.get(m.User, UUID(admin["id"])).is_active = False
        db.commit()
    assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_login_throttling_and_registration_cannot_escalate(client, auth_env, monkeypatch):
    await setup(client)
    for _ in range(5):
        assert (await client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})).status_code == 429
    monkeypatch.setattr(settings, "auth_allow_registration", True)
    response = await client.post("/api/auth/register", json={"username": "member", "password": PASSWORD, "role": "owner"})
    assert response.status_code == 201 and response.json()["role"] == "developer"
    assert (await client.post("/api/auth/users", headers=csrf(client), json={"username": "hacker", "password": PASSWORD})).status_code == 403
    assert (await client.post("/api/auth/register", json={"username": "MEMBER", "password": PASSWORD})).status_code == 409


@pytest.mark.asyncio
async def test_cross_user_http_isolation_before_services(client, auth_env):
    admin = await setup(client)
    other = await add_user(client, "other")
    a = seed_user_data(auth_env[1], admin["id"])
    b = seed_user_data(auth_env[1], other["id"])
    assert (await client.post("/api/auth/login", json={"username": "other", "password": PASSWORD})).status_code == 200
    repos = (await client.get("/api/repos")).json()
    assert [row["id"] for row in repos] == [b["repo"]]
    assert [row["id"] for row in (await client.get("/api/workspaces")).json()] == [b["workspace"]]
    assert [row["id"] for row in (await client.get("/api/action-drafts")).json()] == [b["draft"]]
    assert [row["id"] for row in (await client.get("/api/action-drafts/audit-logs")).json()] == [b["audit"]]
    assert [row["eval_id"] for row in (await client.get("/api/evals")).json()] == [b["evaluation"]]
    assert [row["id"] for row in (await client.get("/api/chat/workflow-runs")).json()] == [b["workflow"]]
    own_history = await client.get(f"/api/chat/history?repo_id={b['repo']}&conversation_id={b['conversation']}")
    assert own_history.status_code == 200
    assert [row["id"] for row in own_history.json()] == [b["message"]]
    for method, path, body in [
        ("GET", f"/api/repos/{a['repo']}/conversations", None),
        ("GET", f"/api/chat/history?repo_id={b['repo']}&conversation_id={a['conversation']}", None),
        ("GET", f"/api/issues/{a['issue']}", None),
        ("GET", f"/api/pull-requests/{a['pr']}", None),
        ("GET", f"/api/workflow-runs/{a['run']}", None),
        ("GET", f"/api/evals/{a['evaluation']}", None),
        ("GET", f"/api/action-drafts/{a['draft']}", None),
        ("GET", f"/api/chat/feedback/trace/{a['trace']}", None),
        ("POST", f"/api/action-drafts/{a['draft']}/confirm", {}),
        ("DELETE", f"/api/repos/{a['repo']}", None),
        ("POST", f"/api/rag/{a['repo']}/search", {"repo_id": b["repo"], "query": "secret"}),
        ("GET", f"/api/rag/repositories/{b['repo']}/documents/{a['doc']}/chunks", None),
        ("POST", f"/api/rag/repositories/{b['repo']}/documents/{a['doc']}/retry", {}),
        ("POST", "/api/evals/rag/run", {"repo_id": a["repo"]}),
        ("POST", "/api/chat", {"repo_id": a["repo"], "message": "secret"}),
        ("POST", "/api/reports/weekly", {"repo_id": a["repo"]}),
        ("POST", "/api/workspaces/multi-repo-report", {"workspace_id": a["workspace"]}),
        ("POST", "/api/workspaces", {"name": "forged", "repo_ids": [a["repo"]]}),
        ("POST", "/api/action-drafts", {"repo_id": b["repo"], "target_type": "issue", "target_id": a["issue"], "content": "forged"}),
    ]:
        response = await client.request(method, path, json=body, headers=csrf(client))
        assert response.status_code == 404, (method, path, response.text)
    assert (await client.get(f"/api/chat/feedback/trace/{b['trace']}")).status_code == 200
    assert (await client.get("/api/repos")).headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", ["project_doc", "project_overview", "memory_note", "knowledge_file"])
async def test_rag_chunks_use_knowledge_source_id(client, auth_env, source_type):
    admin = await setup(client)
    data = seed_user_data(auth_env[1], admin["id"])
    source_id = uuid4()
    with auth_env[1]() as db:
        db.add_all([
            m.Document(repo_id=UUID(data["repo"]), source_type=source_type, source_id=source_id,
                       title="Indexed material", content=f"Chunk {index}", meta={"chunk_index": index})
            for index in range(2)
        ])
        db.commit()
        assert db.get(m.KnowledgeSourceDocument, source_id) is None

    # Follow the same list -> preview flow as the RAG workbench.
    listing = await client.get(f"/api/repos/{data['repo']}/knowledge")
    assert listing.status_code == 200, listing.text
    item = next(row for row in listing.json() if row["id"] == str(source_id))
    assert item["chunk_count"] == 2
    response = await client.get(f"/api/rag/repositories/{data['repo']}/documents/{item['id']}/chunks")
    assert response.status_code == 200, response.text
    assert {row["content"] for row in response.json()} == {"Chunk 0", "Chunk 1"}
    assert {row["position"] for row in response.json()} == {1, 2}


@pytest.mark.asyncio
async def test_rag_chunks_keep_upload_compatibility_and_repository_isolation(client, auth_env):
    admin = await setup(client)
    other = await add_user(client, "other")
    own = seed_user_data(auth_env[1], admin["id"])
    same_user_repo = seed_user_data(auth_env[1], admin["id"], repo_name="second")
    foreign = seed_user_data(auth_env[1], other["id"])
    own_source, foreign_source = uuid4(), uuid4()
    with auth_env[1]() as db:
        for data, source_id, source_type in [
            (own, own_source, "project_doc"),
            (foreign, foreign_source, "project_doc"),
            (own, UUID(own["doc"]), "knowledge_file"),
        ]:
            db.add(m.Document(repo_id=UUID(data["repo"]), source_type=source_type,
                              source_id=source_id, title="Private", content="private chunk"))
        db.commit()

    response = await client.get(f"/api/rag/repositories/{own['repo']}/documents/{own['doc']}/chunks")
    assert response.status_code == 200, response.text
    assert [row["content"] for row in response.json()] == ["private chunk"]
    # A real upload can be queued and legitimately have no chunks yet.
    response = await client.get(f"/api/rag/repositories/{same_user_repo['repo']}/documents/{same_user_repo['doc']}/chunks")
    assert response.status_code == 200 and response.json() == []

    for repo_id, source_id in [
        (own["repo"], uuid4()),
        (same_user_repo["repo"], own_source),
        (same_user_repo["repo"], own["doc"]),
        (own["repo"], foreign_source),
        (foreign["repo"], foreign_source),
    ]:
        response = await client.get(f"/api/rag/repositories/{repo_id}/documents/{source_id}/chunks")
        assert response.status_code == 404, (repo_id, source_id, response.text)

    await client.post("/api/auth/login", json={"username": "other", "password": PASSWORD})
    response = await client.get(f"/api/rag/repositories/{foreign['repo']}/documents/{foreign_source}/chunks")
    assert response.status_code == 200, response.text
    assert [row["content"] for row in response.json()] == ["private chunk"]
    response = await client.get(f"/api/rag/repositories/{foreign['repo']}/documents/{own_source}/chunks")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_same_upstream_keeps_independent_tokens_and_checkouts(client, auth_env, monkeypatch, tmp_path):
    from app.api.routes.chat import _repo_checkout_path, _workspace_root
    from app.core.security import decrypt_token
    from app.services.github.client import GitHubClient

    admin = await setup(client)
    other = await add_user(client, "other")
    payload = {"owner": "example", "repo": "shared", "demo_mode": True, "token": "token-A"}
    a = await client.post("/api/repos/connect", headers=csrf(client), json=payload)
    assert a.status_code == 200, a.text
    await client.post("/api/auth/login", json={"username": "other", "password": PASSWORD})
    b = await client.post("/api/repos/connect", headers=csrf(client), json={**payload, "token": "token-B"})
    assert b.status_code == 200, b.text
    assert a.json()["repo_id"] != b.json()["repo_id"]
    assert "token-B" not in b.text
    # An ordinary owner is a tenant, not a server filesystem administrator.
    for override in ({"local_path": str(tmp_path)}, {"clone_parent_dir": str(tmp_path)}, {"api_base_url": "http://localhost:9000"}):
        denied = await client.post("/api/repos/connect", headers=csrf(client), json={**payload, **override})
        assert denied.status_code == 403
    monkeypatch.setattr(settings, "github_token", "server-secret")
    assert "Authorization" not in GitHubClient()._headers()
    monkeypatch.setattr(settings, "repo_checkout_dir", str(tmp_path))
    with auth_env[1]() as db:
        repos = {str(repo.owner_user_id): repo for repo in db.query(m.Repository)}
        a_repo, b_repo = repos[admin["id"]], repos[other["id"]]
        assert decrypt_token(a_repo.github_token_encrypted, settings.token_encryption_key) == "token-A"
        assert decrypt_token(b_repo.github_token_encrypted, settings.token_encryption_key) == "token-B"
        a_checkout = _repo_checkout_path(a_repo)
        a_checkout.mkdir()
        assert _repo_checkout_path(b_repo) != a_checkout
        with pytest.raises(ValueError, match="还没有克隆"):
            _workspace_root(b_repo)


@pytest.mark.asyncio
async def test_webhook_is_only_public_with_valid_hmac(client, monkeypatch):
    from app.api.routes import webhooks

    monkeypatch.setattr(settings, "github_webhook_secret", None)
    assert (await client.post("/api/webhooks/github", json={})).status_code == 503
    monkeypatch.setattr(settings, "github_webhook_secret", "webhook-secret")
    body = json.dumps({"repository": {"full_name": "example/shared"}}).encode()
    assert (await client.post("/api/webhooks/github", content=body)).status_code == 401
    ids = [uuid4(), uuid4()]
    monkeypatch.setattr(webhooks, "find_repos_by_full_name", lambda _name: [SimpleNamespace(id=key) for key in ids])
    scheduled = []

    async def sync(repo_id, _payload, **_kwargs):
        scheduled.append(repo_id)

    monkeypatch.setattr(webhooks, "sync_repository_by_id", sync)
    headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest(),
               "X-GitHub-Event": "push"}
    response = await client.post("/api/webhooks/github", content=body, headers=headers)
    assert response.status_code == 200 and response.json()["repo_ids"] == [str(key) for key in ids]
    assert scheduled == ids
    headers["X-GitHub-Event"] = "ping"
    response = await client.post("/api/webhooks/github", content=body, headers=headers)
    assert response.status_code == 200 and response.json()["action"] == "ignored"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "developer"])
async def test_roles_restrict_tools_and_confirmation(client, auth_env, role):
    await setup(client)
    user = await add_user(client, role, role)
    data = seed_user_data(auth_env[1], user["id"])
    await client.post("/api/auth/login", json={"username": role, "password": PASSWORD})
    assert (await client.get("/api/repos")).status_code == 200
    assert (await client.post(f"/api/action-drafts/{data['draft']}/confirm", headers=csrf(client), json={})).status_code == 403
    draft = await client.post("/api/action-drafts", headers=csrf(client),
                              json={"repo_id": data["repo"], "content": "draft"})
    assert draft.status_code == (403 if role == "viewer" else 200), draft.text
    if role == "viewer":
        assert (await client.post("/api/chat", headers=csrf(client), json={"repo_id": data["repo"], "message": "run tools"})).status_code == 403


@pytest.mark.asyncio
async def test_orm_scope_alias_count_bulk_dml_and_foreign_keys(client, auth_env):
    a_user = await setup(client)
    b_user = await add_user(client, "other")
    a = seed_user_data(auth_env[1], a_user["id"])
    b = seed_user_data(auth_env[1], b_user["id"])
    with auth_env[1]() as db:
        bind_user(db, db.get(m.User, UUID(b_user["id"])))
        alias = aliased(m.Repository)
        assert db.scalar(select(func.count()).select_from(alias)) == 1
        for model in (m.Repository, m.Conversation, m.Issue, m.PullRequest, m.WorkflowRun,
                      m.ChatMessage, m.KnowledgeSourceDocument, m.ActionDraft, m.Workspace,
                      m.AuditLog, m.AnalysisResult, m.EvalRun, m.PRFile, m.PRReviewComment,
                      m.AgentWorkflowRun, m.AgentTaskRun):
            assert db.query(model).count() == 1, model.__name__
        assert db.get(m.Repository, UUID(a["repo"])) is None
        assert db.query(m.PullRequest).one().files[0].id == UUID(b["pr_file"])
        changed = db.execute(update(m.Issue).values(title="updated"))
        assert changed.rowcount == 1
        removed = db.execute(delete(m.PRReviewComment))
        assert removed.rowcount == 1
        db.commit()
        db.add(m.Conversation(repo_id=UUID(a["repo"]), title="forged"))
        with pytest.raises(HTTPException) as rejected:
            db.flush()
        assert rejected.value.status_code == 404
        db.rollback()
        repo = m.Repository(owner="example", name="new", full_name="example/new")
        db.add(repo)
        db.flush()
        assert repo.owner_user_id == UUID(b_user["id"])
        repo.owner_user_id = UUID(a_user["id"])
        with pytest.raises(HTTPException):
            db.flush()
        db.rollback()
    with auth_env[1]() as db:
        assert db.get(m.Issue, UUID(a["issue"])).title == "private issue"
        assert db.get(m.PRReviewComment, UUID(a["comment"])) is not None


@pytest.mark.parametrize("unique_style", ["index", "constraint"])
def test_legacy_schema_upgrade_preserves_data_and_allows_independent_repos(unique_style):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    user_id, repo_id = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id CHAR(32) PRIMARY KEY, name VARCHAR(200), role VARCHAR(80), github_token_encrypted TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"))
        unique = " UNIQUE" if unique_style == "constraint" else ""
        conn.execute(text(f"CREATE TABLE repositories (id CHAR(32) PRIMARY KEY, owner VARCHAR(200), name VARCHAR(200), full_name VARCHAR(400){unique})"))
        if unique_style == "index":
            conn.execute(text("CREATE UNIQUE INDEX ix_repositories_full_name ON repositories (full_name)"))
        conn.execute(text("INSERT INTO users (id, name, role) VALUES (:id, 'demo', 'owner')"), {"id": user_id.hex})
        conn.execute(text("INSERT INTO repositories (id, owner, name, full_name) VALUES (:id, 'old', 'repo', 'old/repo')"), {"id": repo_id.hex})
    m.Base.metadata.create_all(engine)
    migrate_auth_schema(engine)
    migrate_auth_schema(engine)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT full_name FROM repositories WHERE id=:id"), {"id": repo_id.hex}).scalar_one() == "old/repo"
        conn.execute(text("UPDATE repositories SET owner_user_id=:owner"), {"owner": user_id.hex})
        conn.execute(text("INSERT INTO repositories (id, owner, name, full_name, owner_user_id) VALUES (:id, 'old', 'repo', 'old/repo', :owner)"), {"id": uuid4().hex, "owner": uuid4().hex})
    engine.dispose()

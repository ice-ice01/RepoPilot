import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.repos import _clone_target_from_parent, _infer_provider, _parse_git_remote_url
from app.schemas.repos import RepoConnectRequest


def test_git_helpers_decode_unicode_paths_as_utf8(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.api.routes.repos import _git_output
    from app.services.code_analysis import _git_text

    unicode_path = "D:/Desktop/\u8863\u6a71\u9879\u76ee/wardrobeAgent"

    def fake_run(args, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return SimpleNamespace(stdout=unicode_path + "\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _git_output(["rev-parse", "--show-toplevel"], tmp_path) == unicode_path
    assert _git_text(["rev-parse", "--show-toplevel"], tmp_path) == unicode_path


@pytest.mark.parametrize(
    ("remote_url", "owner", "repo", "host"),
    [
        ("https://github.com/openai/codex.git", "openai", "codex", "github.com"),
        ("git@github.com:openai/codex.git", "openai", "codex", "github.com"),
        ("ssh://git@github.example.com/platform/devflow.git", "platform", "devflow", "github.example.com"),
    ],
)
def test_parse_git_remote_url(remote_url: str, owner: str, repo: str, host: str) -> None:
    parsed = _parse_git_remote_url(remote_url)

    assert parsed.owner == owner
    assert parsed.repo == repo
    assert parsed.host == host


def test_infer_provider_for_custom_remote_host() -> None:
    assert _infer_provider("github", "github.example.com") == "github_compatible"
    assert _infer_provider("github", "github.com") == "github"


def test_clone_target_from_parent_rejects_occupied_directory(tmp_path) -> None:
    target = tmp_path / "codex"
    target.mkdir()
    (target / "README.md").write_text("# occupied\n", encoding="utf-8")

    with pytest.raises(HTTPException):
        _clone_target_from_parent(str(tmp_path), "codex")


def test_repo_connect_request_uses_a_small_bounded_initial_sync() -> None:
    payload = RepoConnectRequest(owner="openai", repo="openai-quickstart-python")

    assert payload.initial_sync_limit == 5
    assert payload.sync_issues is True
    assert payload.sync_pull_requests is True
    assert payload.sync_workflow_runs is True

    with pytest.raises(ValidationError):
        RepoConnectRequest(owner="openai", repo="openai-quickstart-python", initial_sync_limit=0)

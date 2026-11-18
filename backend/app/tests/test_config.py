import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolated_ai_environment(monkeypatch):
    for name in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_ai_environment_has_priority(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-ai-key")
    monkeypatch.setenv("AI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-ai-model")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.test/v1")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    config = Settings(_env_file=None)
    assert config.llm_api_key == "test-ai-key"
    assert config.llm_base_url == "https://example.test/v1"
    assert config.llm_model == "test-ai-model"


def test_legacy_environment_remains_supported(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.test/v1")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    config = Settings(_env_file=None)
    assert config.llm_api_key == "legacy-key"
    assert config.llm_base_url == "https://legacy.test/v1"
    assert config.llm_model == "legacy-model"


def test_explicit_field_initialization_remains_supported():
    config = Settings(_env_file=None, llm_api_key="explicit", llm_base_url="https://example.test", llm_model="explicit-model")
    assert config.llm_api_key == "explicit"
    assert config.llm_base_url == "https://example.test/v1"
    assert config.llm_model == "explicit-model"


def test_demo_can_explicitly_disable_ai(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    assert not Settings(_env_file=None).llm_api_key


@pytest.mark.parametrize("base_url, expected", [
    ("https://example.test", "https://example.test/v1"),
    ("https://example.test/", "https://example.test/v1"),
    ("https://example.test/v1/", "https://example.test/v1"),
    ("https://example.test/custom/v1", "https://example.test/custom/v1"),
])
def test_api_base_url_normalization(monkeypatch, base_url, expected):
    monkeypatch.setenv("AI_BASE_URL", base_url)
    assert Settings(_env_file=None).llm_base_url == expected


def test_planner_defaults_and_environment_overrides(monkeypatch):
    monkeypatch.delenv("PLANNER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PLANNER_MAX_TOKENS", raising=False)
    config = Settings(_env_file=None)
    assert config.planner_timeout_seconds == 30
    assert config.planner_max_tokens == 2048
    monkeypatch.setenv("PLANNER_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("PLANNER_MAX_TOKENS", "1024")
    config = Settings(_env_file=None)
    assert config.planner_timeout_seconds == 12
    assert config.planner_max_tokens == 1024


@pytest.mark.parametrize("values", [
    {"planner_timeout_seconds": 0}, {"planner_timeout_seconds": 181},
    {"planner_max_tokens": 255}, {"planner_max_tokens": 8193},
])
def test_planner_limits_are_validated(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)

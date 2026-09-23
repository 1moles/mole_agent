"""多供应商 / 多模型选择：配置解析、选择规则、启动优先级、运行中切换。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mole_agent import config as config_mod
from mole_agent.config import Settings, load_settings
from mole_agent.models import (
    ModelCatalog,
    ModelSelectionError,
    load_catalog,
    load_last_model,
    save_last_model,
)

MODELS_TOML = textwrap.dedent("""
    default = "deepseek/deepseek-flash"

    [providers.deepseek]
    client_provider = "DeepSeek"
    api_base = "https://api.deepseek.com/v1"
    api_key_env = "TEST_DEEPSEEK_KEY"
    models = ["deepseek-flash", "deepseek-v4-pro"]

    [providers.qwen]
    client_provider = "DashScope"
    api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env = "TEST_QWEN_KEY"
    models = ["qwen3-coder-next", "shared-model"]
    max_tokens = 4096
    temperature = 0.1

    [providers.gateway]
    client_provider = "OpenAI"
    api_base = "https://gw.example.com/v1"
    api_key = "none"
    verify_ssl = false
    headers = { "X-Tenant" = "team-a" }
    models = ["shared-model"]

    [providers.openrouter]
    client_provider = "OpenRouter"
    api_base = "https://openrouter.ai/api/v1"
    api_key_env = "TEST_OPENROUTER_KEY"
""")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """隔离环境：不读用户真实的 .env / models.toml / state.json。"""
    home = tmp_path / "home"
    pkg = tmp_path / "pkg"
    home.mkdir()
    pkg.mkdir()
    monkeypatch.setattr(config_mod, "PACKAGE_ROOT", pkg)
    monkeypatch.setenv("MOLE_HOME", str(home))
    for var in ("MOLE_MODEL", "MOLE_PROVIDER", "MOLE_API_BASE", "MOLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEST_DEEPSEEK_KEY", "sk-ds")
    monkeypatch.setenv("TEST_QWEN_KEY", "sk-qw")
    monkeypatch.delenv("TEST_OPENROUTER_KEY", raising=False)
    (pkg / "models.toml").write_text(MODELS_TOML, encoding="utf-8")
    return home, pkg


def _catalog(pkg: Path) -> ModelCatalog:
    return load_catalog([pkg / "models.toml"])


# ---------------------------------------------------------------- 选择规则
def test_resolve_rules(env):
    _, pkg = env
    cat = _catalog(pkg)
    assert cat.resolve(None).spec == "deepseek/deepseek-flash"                   # default
    assert cat.resolve("qwen").spec == "qwen/qwen3-coder-next"                   # 只写供应商
    assert cat.resolve("deepseek/any-new-model").spec == "deepseek/any-new-model"  # 不在列表里也行
    assert cat.resolve("deepseek-v4-pro").spec == "deepseek/deepseek-v4-pro"     # 只写模型
    nested = cat.resolve("openrouter/anthropic/claude-x")                         # 模型名里带 /
    assert nested.provider.name == "openrouter" and nested.model == "anthropic/claude-x"
    with pytest.raises(ModelSelectionError, match="多个供应商"):
        cat.resolve("shared-model")
    with pytest.raises(ModelSelectionError, match="找不到"):
        cat.resolve("nope")
    with pytest.raises(ModelSelectionError, match="没有配置 models"):
        cat.resolve("openrouter")


def test_provider_keys_and_problems(env):
    _, pkg = env
    cat = _catalog(pkg)
    assert cat.providers["deepseek"].resolved_api_key == "sk-ds"
    assert cat.providers["gateway"].problems() == []
    assert "TEST_OPENROUTER_KEY" in cat.providers["openrouter"].problems()[0]


def test_invalid_client_provider(tmp_path: Path):
    bad = tmp_path / "models.toml"
    bad.write_text('[providers.x]\nclient_provider = "Foo"\napi_base = "http://x"\n', encoding="utf-8")
    with pytest.raises(ModelSelectionError, match="不被 openjiuwen 支持"):
        load_catalog([bad])


def test_first_file_wins(tmp_path: Path):
    a, b = tmp_path / "a.toml", tmp_path / "b.toml"
    a.write_text('[providers.p]\napi_base = "http://a"\napi_key = "k"\nmodels = ["m"]\n', encoding="utf-8")
    b.write_text('default = "p/m"\n[providers.p]\napi_base = "http://b"\n[providers.q]\napi_base = "http://q"\n',
                 encoding="utf-8")
    cat = load_catalog([a, b])
    assert cat.providers["p"].api_base == "http://a" and "q" in cat.providers
    assert cat.default == "p/m"


def test_legacy_env_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOLE_MODEL", "old-model")
    monkeypatch.setenv("MOLE_PROVIDER", "OpenAI")
    monkeypatch.setenv("MOLE_API_BASE", "http://legacy/v1")
    monkeypatch.setenv("MOLE_API_KEY", "sk-old")
    cat = load_catalog([tmp_path / "missing.toml"])
    choice = cat.resolve(None)
    assert choice.spec == "default/old-model" and choice.provider.resolved_api_key == "sk-old"


# ---------------------------------------------------------------- 启动优先级：-m > 上次切换 > default
def test_startup_priority(env):
    home, _ = env
    assert load_settings().model_spec == "deepseek/deepseek-flash"

    save_last_model(home / "state.json", "qwen/qwen3-coder-next")
    assert load_last_model(home / "state.json") == "qwen/qwen3-coder-next"
    s = load_settings()
    assert s.model_spec == "qwen/qwen3-coder-next"
    assert s.provider == "DashScope" and s.api_key == "sk-qw"
    assert s.max_tokens == 4096 and s.temperature == 0.1   # 供应商级覆盖

    assert load_settings(model="deepseek/deepseek-v4-pro").model_spec == "deepseek/deepseek-v4-pro"

    save_last_model(home / "state.json", "removed/provider")   # 失效的记录 → 回落到 default
    assert load_settings().model_spec == "deepseek/deepseek-flash"


def test_explicit_bad_model_is_reported(env):
    s = load_settings(model="nope")
    assert any("找不到" in p for p in s.validate())


def test_missing_key_is_reported(env):
    s = load_settings(model="openrouter/some-model")
    assert any("TEST_OPENROUTER_KEY" in p for p in s.validate())


def test_apply_choice_restores_defaults(env):
    _, pkg = env
    cat = _catalog(pkg)
    s = Settings(max_tokens=8192, temperature=None, verify_ssl=True)
    s.apply_choice(cat.resolve("gateway/shared-model"))
    assert s.verify_ssl is False and s.custom_headers == {"X-Tenant": "team-a"}
    s.apply_choice(cat.resolve("qwen"))
    assert s.max_tokens == 4096 and s.verify_ssl is True and s.custom_headers == {}
    s.apply_choice(cat.resolve("deepseek"))
    assert s.max_tokens == 8192 and s.temperature is None   # 回落到全局默认


# ---------------------------------------------------------------- 模型菜单
def test_model_menu_numbering(env):
    from mole_agent.cli import print_model_menu, usable_choices

    settings = load_settings()
    choices = print_model_menu(settings)
    assert [c.spec for c in usable_choices(settings)] == [c.spec for c in choices]
    specs = [c.spec for c in choices]
    # openrouter 没 key，不参与编号；gateway 与 qwen 的 shared-model 各占一个编号
    assert specs == ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro",
                     "qwen/qwen3-coder-next", "qwen/shared-model", "gateway/shared-model"]


def test_legacy_env_merged_with_toml(env, monkeypatch):
    _, pkg = env
    monkeypatch.setenv("MOLE_MODEL", "old-model")
    monkeypatch.setenv("MOLE_API_BASE", "http://legacy/v1")
    monkeypatch.setenv("MOLE_API_KEY", "sk-old")
    cat = _catalog(pkg)
    assert cat.default == "deepseek/deepseek-flash"          # toml 的 default 优先
    assert cat.resolve("default").spec == "default/old-model"  # 旧配置仍可选

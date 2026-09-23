"""请求头鉴权（auth = "headers"）：配置解析、校验，以及真正发出去的 HTTP 请求头。

后半部分起一个本地的假 OpenAI 兼容服务，记录收到的请求头，验证：
带上了配置的请求头（${VAR} 已替换成环境变量的值），并且没有发 Authorization。
"""

from __future__ import annotations

import json
import threading
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mole_agent import cli
from mole_agent.agent import build_model
from mole_agent.config import Settings
from mole_agent.models import ModelSelectionError, ProviderConfig, list_remote_models, load_catalog

MODELARTS_TOML = textwrap.dedent("""
    [providers.modelarts]
    client_provider = "ModelArts"
    api_base = "{base}"
    auth = "headers"
    headers = {{ "X-Auth-Token" = "${{TEST_MA_TOKEN}}", "X-Project-Id" = "proj-${{TEST_MA_PROJECT}}" }}
    models = ["qwen3-32b"]
""")


def _catalog(tmp_path: Path, base: str = "https://infer.example.com/v1", body: str = MODELARTS_TOML):
    path = tmp_path / "models.toml"
    path.write_text(body.format(base=base), encoding="utf-8")
    return load_catalog([path])


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch):
    for var in ("MOLE_MODEL", "MOLE_PROVIDER", "MOLE_API_BASE", "MOLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- 配置解析与校验
def test_header_auth_resolves_env_vars(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_MA_TOKEN", "tok-123")
    monkeypatch.setenv("TEST_MA_PROJECT", "42")
    p = _catalog(tmp_path).providers["modelarts"]
    assert p.auth == "headers"
    assert p.headers["X-Auth-Token"] == "${TEST_MA_TOKEN}"            # models.toml 里只有变量名
    assert p.resolved_headers == {"X-Auth-Token": "tok-123", "X-Project-Id": "proj-42"}
    assert p.problems() == []                                          # 不需要 API key


def test_missing_header_env_is_reported(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TEST_MA_TOKEN", raising=False)
    monkeypatch.delenv("TEST_MA_PROJECT", raising=False)
    p = _catalog(tmp_path).providers["modelarts"]
    assert p.missing_env() == ["TEST_MA_TOKEN", "TEST_MA_PROJECT"]
    problems = "；".join(p.problems())
    assert "TEST_MA_TOKEN" in problems and "API key" not in problems


def test_auth_is_inferred_from_headers_only(tmp_path: Path):
    body = '[providers.gw]\napi_base = "{base}"\nheaders = {{ "X-Apig-AppCode" = "${{APP}}" }}\n'
    assert _catalog(tmp_path, body=body).providers["gw"].auth == "headers"
    body = '[providers.gw]\napi_base = "{base}"\napi_key_env = "K"\nheaders = {{ "X-Tenant" = "t" }}\n'
    assert _catalog(tmp_path, body=body).providers["gw"].auth == "api_key"   # 有 key 时请求头只是附加的


def test_invalid_auth_config(tmp_path: Path):
    with pytest.raises(ModelSelectionError, match="auth='token'"):
        _catalog(tmp_path, body='[providers.x]\napi_base = "{base}"\nauth = "token"\n')
    with pytest.raises(ModelSelectionError, match="headers 应写成表"):
        _catalog(tmp_path, body='[providers.x]\napi_base = "{base}"\nheaders = "X-Auth-Token: a"\n')
    assert "没有配置 headers" in "；".join(ProviderConfig(name="x", api_base="http://x", auth="headers").problems())


def test_authorization_header_is_flagged(monkeypatch):
    """openjiuwen 会丢弃自定义的 Authorization 请求头，要提前报出来，而不是静默失效。"""
    p = ProviderConfig(name="x", api_base="http://x", auth="headers", headers={"Authorization": "Basic abc"})
    assert any("Authorization" in msg and "丢弃" in msg for msg in p.problems())


def test_settings_and_model_use_custom_headers_auth(tmp_path: Path, monkeypatch):
    from openjiuwen.core.foundation.llm import LLMAuthMode

    monkeypatch.setenv("TEST_MA_TOKEN", "tok-123")
    monkeypatch.setenv("TEST_MA_PROJECT", "42")
    settings = Settings(model="x", api_key="old-key", custom_headers={"X-Old": "1"})
    settings.apply_choice(_catalog(tmp_path).resolve("modelarts"))
    assert settings.auth == "headers" and settings.api_key == ""
    assert settings.custom_headers == {"X-Auth-Token": "tok-123", "X-Project-Id": "proj-42"}
    assert settings.validate() == []                                  # 不再要求 API key

    cfg = build_model(settings).model_client_config
    assert cfg.auth_mode in (LLMAuthMode.CustomHeaders, LLMAuthMode.CustomHeaders.value)
    assert cfg.custom_headers == {"X-Auth-Token": "tok-123", "X-Project-Id": "proj-42"}

    settings.auth, settings.api_key, settings.custom_headers = "api_key", "sk", {}
    assert build_model(settings).model_client_config.auth_mode in (LLMAuthMode.ApiKey, LLMAuthMode.ApiKey.value)
    settings.auth, settings.api_key = "none", ""
    assert build_model(settings).model_client_config.auth_mode in (LLMAuthMode.NoneAuth, LLMAuthMode.NoneAuth.value)


def test_describe_auth_never_shows_values():
    s = Settings(auth="headers", custom_headers={"X-Auth-Token": "secret-token-value"})
    text = cli.describe_auth(s)
    assert "X-Auth-Token" in text and "secret-token-value" not in text and "不发 Authorization" in text


# ---------------------------------------------------------------- 真实 HTTP：看发出去的请求头
class _FakeOpenAI(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):  # 不往测试输出里打访问日志
        pass

    def _record(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        entry = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
        type(self).requests.append(entry)
        return entry

    def _send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._record()
        self._send_json({"object": "list", "data": [{"id": "qwen3-32b", "object": "model"}]})

    def do_POST(self):  # noqa: N802
        entry = self._record()
        message = {"role": "assistant", "content": "你好"}
        if not entry["body"].get("stream"):
            self._send_json({
                "id": "c1", "object": "chat.completion", "created": 0, "model": "qwen3-32b",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in (({"role": "assistant", "content": "你好"}, None), ({}, "stop")):
            chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "qwen3-32b",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture()
def fake_server(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    _FakeOpenAI.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", _FakeOpenAI.requests
    finally:
        server.shutdown()


async def test_requests_carry_headers_and_no_authorization(tmp_path: Path, monkeypatch, fake_server):
    base, requests = fake_server
    monkeypatch.setenv("TEST_MA_TOKEN", "tok-123")
    monkeypatch.setenv("TEST_MA_PROJECT", "42")
    catalog = _catalog(tmp_path, base=base)
    settings = Settings(project_dir=tmp_path, home_dir=tmp_path / "home")
    settings.catalog = catalog
    settings.apply_choice(catalog.resolve("modelarts"))

    assert await cli._check(settings) == 0                          # mole --check 能调通
    model = build_model(settings)
    chunks = [c async for c in model.stream([{"role": "user", "content": "hi"}])]   # agent 实际走的流式调用
    assert "你好" in "".join(str(getattr(c, "content", "") or "") for c in chunks)
    assert await list_remote_models(catalog.providers["modelarts"]) == ["qwen3-32b"]   # /models 在线查询

    posts = [r for r in requests if r["path"].endswith("/chat/completions")]
    assert len(posts) >= 2 and any(r["body"].get("stream") for r in posts)
    for r in requests:
        assert r["headers"].get("x-auth-token") == "tok-123", r["path"]
        assert r["headers"].get("x-project-id") == "proj-42"
        assert "authorization" not in r["headers"], r["path"]
    assert posts[0]["body"]["model"] == "qwen3-32b"


async def test_api_key_mode_still_sends_bearer(tmp_path: Path, monkeypatch, fake_server):
    base, requests = fake_server
    monkeypatch.setenv("TEST_GW_KEY", "sk-abc")
    body = '[providers.gw]\napi_base = "{base}"\napi_key_env = "TEST_GW_KEY"\nheaders = {{ "X-Tenant" = "t1" }}\nmodels = ["m"]\n'
    catalog = _catalog(tmp_path, base=base, body=body)
    settings = Settings(project_dir=tmp_path, home_dir=tmp_path / "home")
    settings.apply_choice(catalog.resolve("gw"))
    assert await cli._check(settings) == 0
    assert requests[-1]["headers"]["authorization"] == "Bearer sk-abc"
    assert requests[-1]["headers"]["x-tenant"] == "t1"

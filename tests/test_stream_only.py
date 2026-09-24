"""只接受流式请求的模型网关（models.toml 里 stream_only = true）。

起一个本地假网关：非流式请求直接返回 400，流式请求把回复拆成多个分片发回。验证：
打开 stream_only 后，invoke（mole --check、任务完成判断等都走它）也能拿到完整回复，
带解析器的调用能解析出结果，工具调用的分片能拼回完整参数；不打开时会被网关拒绝。
上下文压缩器自己建了一个普通 Model，由 CompressionWatcher 另外换成流式（/compact 和自动压缩都覆盖）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mole_agent import cli
from mole_agent.agent import StreamOnlyModel, build_model
from mole_agent.config import Settings
from mole_agent.models import ModelSelectionError, load_catalog


def _catalog(tmp_path: Path, base: str, extra: str = "stream_only = true"):
    path = tmp_path / "models.toml"
    path.write_text(
        f'[providers.inner]\napi_base = "{base}"\napi_key = "k"\n{extra}\nmodels = ["inner-model"]\n',
        encoding="utf-8",
    )
    return load_catalog([path])


def _settings(tmp_path: Path, base: str, extra: str = "stream_only = true") -> Settings:
    catalog = _catalog(tmp_path, base, extra)
    settings = Settings(project_dir=tmp_path, home_dir=tmp_path / "home")
    settings.catalog = catalog
    settings.apply_choice(catalog.resolve("inner"))
    return settings


@pytest.fixture(autouse=True)
def _no_legacy_env(monkeypatch):
    for var in ("MOLE_MODEL", "MOLE_PROVIDER", "MOLE_API_BASE", "MOLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- 配置
def test_stream_only_parsing(tmp_path: Path):
    assert _catalog(tmp_path, "http://x/v1").providers["inner"].stream_only is True
    assert _catalog(tmp_path, "http://x/v1", "stream = true").providers["inner"].stream_only is True  # 别名
    assert _catalog(tmp_path, "http://x/v1", "").providers["inner"].stream_only is False
    with pytest.raises(ModelSelectionError, match="stream_only 应为 true 或 false"):
        _catalog(tmp_path, "http://x/v1", 'stream_only = "yes"')


def test_build_model_picks_stream_only_model(tmp_path: Path):
    settings = _settings(tmp_path, "http://x/v1")
    model = build_model(settings)
    assert isinstance(model, StreamOnlyModel)
    assert "stream" not in model.model_config.model_dump()          # 没有把 stream 塞进请求参数
    settings.stream_only = False
    assert not isinstance(build_model(settings), StreamOnlyModel)


# ---------------------------------------------------------------- 假网关：只接受流式请求
class _StreamOnlyGateway(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).requests.append(body)
        if body.get("stream") is not True:
            data = json.dumps({"error": {"message": "only stream=true is supported", "code": 400}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        last = str(body["messages"][-1].get("content", ""))
        if not body.get("tools") and ("<memory_block" in last or "compress" in last.lower() or "压缩" in last):
            deltas = [{"role": "assistant", "content": "摘要："}, {"content": "用户打过几次招呼"}]   # 上下文压缩
            finish = "stop"
        elif "工具" in last:
            deltas = [
                {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                      "function": {"name": "read_file", "arguments": ""}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": '{"file_path": '}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]},
            ]
            finish = "tool_calls"
        elif "json" in last.lower():
            deltas = [{"role": "assistant", "content": '{"answer": '}, {"content": "42}"}]
            finish = "stop"
        else:
            deltas = [{"role": "assistant", "content": "你"}, {"content": "好"}]
            finish = "stop"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(payload: dict) -> None:
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())

        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "inner-model"}
        for delta in deltas:
            send({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        send({**base, "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture()
def gateway(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    _StreamOnlyGateway.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamOnlyGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", _StreamOnlyGateway.requests
    finally:
        server.shutdown()


async def test_check_and_invoke_work_through_stream(tmp_path: Path, gateway):
    from openjiuwen.core.foundation.llm import AssistantMessage, JsonOutputParser

    base, requests = gateway
    settings = _settings(tmp_path, base)
    assert await cli._check(settings) == 0                            # mole --check 走 invoke

    model = build_model(settings)
    reply = await model.invoke([{"role": "user", "content": "打个招呼"}])
    assert type(reply) is AssistantMessage                            # 返回完整消息，不是分片
    assert reply.content == "你好"

    parsed = await model.invoke([{"role": "user", "content": "请输出 JSON"}], output_parser=JsonOutputParser())
    assert parsed.content == '{"answer": 42}' and parsed.parser_content == {"answer": 42}   # 上下文压缩就是这样调用的

    tool = await model.invoke([{"role": "user", "content": "调用工具"}], tools=[{
        "type": "function",
        "function": {"name": "read_file", "description": "读文件",
                     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
    }])
    assert [(t.name, json.loads(t.arguments)) for t in tool.tool_calls] == [("read_file", {"file_path": "a.py"})]

    assert requests and all(r.get("stream") is True for r in requests)   # 发出去的全是流式请求


async def test_without_stream_only_gateway_rejects_invoke(tmp_path: Path, gateway):
    base, requests = gateway
    settings = _settings(tmp_path, base, extra="")
    assert await cli._check(settings) == 1                            # 不打开 stream_only 就会被拒
    assert requests[-1].get("stream") is not True


async def test_full_agent_turn_over_stream_only_gateway(tmp_path: Path, gateway):
    """整个 agent 跑一轮：主循环、任务完成判断、能力探测等所有模型调用都只发流式请求。"""
    from openjiuwen.core.runner import Runner

    from mole_agent.agent import build_agent

    base, requests = gateway
    settings = _settings(tmp_path, base)
    (tmp_path / "proj").mkdir()
    settings.project_dir = tmp_path / "proj"
    bundle = build_agent(settings)
    repl = cli.Repl(settings, bundle)
    await Runner.start()
    try:
        text = await repl.run_turn("打个招呼")
    finally:
        await Runner.stop()
    assert "你好" in text
    assert len(requests) >= 2   # 除了对话本身，还有 SDK 的图片能力探测（invoke），也改成了流式
    assert requests and all(r.get("stream") is True for r in requests)


async def _chat_then_compact(tmp_path: Path, base: str, extra: str = "stream_only = true") -> None:
    from openjiuwen.core.runner import Runner

    from mole_agent.agent import build_agent

    settings = _settings(tmp_path, base, extra)
    (tmp_path / "proj").mkdir()
    settings.project_dir = tmp_path / "proj"
    repl = cli.Repl(settings, build_agent(settings))
    await Runner.start()
    try:
        for i in range(3):
            await repl.run_turn(f"第 {i} 次，" + "打个招呼，" * 200)
        await repl.handle_slash("/compact")
    finally:
        await Runner.stop()


async def test_compact_over_stream_only_gateway(tmp_path: Path, gateway, capsys):
    base, requests = gateway
    await _chat_then_compact(tmp_path, base)
    out = capsys.readouterr().out
    assert "正在压缩上下文" in out and "完成" in out, out[-500:]
    summaries = [r for r in requests if not r.get("tools") and "compress" in str(r["messages"][-1]).lower()]
    assert summaries                                                  # 压缩模型真的被调用了
    assert all(r.get("stream") is True for r in requests)             # 包括压缩请求在内，全是流式


async def test_compression_rejected_without_stream_only_reports_reason(tmp_path: Path, gateway, capsys):
    """没打开 stream_only：压缩请求被网关拒绝。SDK 只写日志、上下文不变，终端要把原因说出来。"""
    base, requests = gateway
    await _chat_then_compact(tmp_path, base, extra="")
    out = capsys.readouterr().out
    assert "失败，上下文保持原样" in out and "400" in out, out[-500:]
    assert any(r.get("stream") is not True for r in requests)

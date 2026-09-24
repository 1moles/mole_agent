"""上下文窗口：/context、/compact、自动压缩的终端提示、模型报超长后压缩重试。

主模型用 ScriptedModel；压缩器是 SDK 自己建的 Model（连到 api_base），测试里换成 FakeCompressor。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.runner import Runner

from mole_agent import cli
from mole_agent.agent import build_agent
from mole_agent.config import Settings
from mole_agent.context import fmt_tokens, is_zh_context_overflow

from test_e2e_fake_llm import ScriptedModel

# 内容别写成同一句话重复：SDK 的 ModelAnomalyDetectionRail 会把重复输出当成异常重试
LONG_Q = "".join(f"第 {i} 行代码做了第 {i * 7} 件事；" for i in range(120))
LONG_A = "".join(f"第 {i} 点分析，涉及变量 v{i * 3}。" for i in range(80))


class FakeCompressor:
    def __init__(self, reply: str = "<state_snapshot>摘要：用户问了几个关于代码的问题</state_snapshot>",
                 error: Exception | None = None, delay: float = 0) -> None:
        self.reply, self.error, self.delay = reply, error, delay
        self.prompts: list[str] = []

    async def invoke(self, messages=None, **kwargs):
        self.prompts.append(str(getattr((messages or [None])[-1], "content", "")))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            # 和 SDK 的模型客户端一样：调用失败先发 LLM_CALL_ERROR 事件再抛出
            from openjiuwen.core.runner.callback.events import LLMCallEvents

            await Runner.callback_framework.trigger(LLMCallEvents.LLM_CALL_ERROR, model_name="fake",
                                                    is_stream=False, error=self.error)
            raise self.error
        return AssistantMessage(content=self.reply, tool_calls=[])


def context_of(repl: cli.Repl):
    return repl.bundle.agent.react_agent.context_engine.get_context(session_id=repl.session_id)


def system_notes(repl: cli.Repl) -> list[str]:
    from mole_agent.history import load_session

    return [m.content for m in load_session(repl.history.dir, repl.session_id).messages if m.role == "system"]


def use_fake_compressor(repl: cli.Repl, fake: FakeCompressor) -> None:
    for processor in context_of(repl)._processors:
        executor = getattr(processor, "_compression_executor", None)
        if executor is not None:
            executor._model = fake


@pytest.fixture()
async def env(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)

    def make(**overrides):
        settings = Settings(model="fake", api_key="k", api_base="http://127.0.0.1:9/v1",
                            project_dir=proj, home_dir=tmp_path / "home", **overrides)
        bundle = build_agent(settings)
        fake = ScriptedModel()
        model = bundle.agent.deep_config.model
        model.stream, model.invoke = fake.stream, fake.invoke
        return cli.Repl(settings, bundle), fake

    await Runner.start()
    try:
        yield make
    finally:
        await Runner.stop()


async def chat(repl: cli.Repl, fake: ScriptedModel, n: int, answer: str = LONG_A) -> None:
    for i in range(n):
        fake.plan((f"第 {i} 个回答：{answer}", []))
        await repl.run_interruptible(f"第 {i} 个问题：{LONG_Q}")


# ---------------------------------------------------------------- /context
async def test_context_before_and_after_chat(env, capsys):
    repl, fake = env()
    await repl.handle_slash("/context")                     # 还没发过消息
    out = capsys.readouterr().out
    assert "约 0 / 200k" in out and "到 80%（约 160k）自动压缩" in out
    assert "默认值：fake 不在 SDK 内置的模型表里" in out

    await chat(repl, fake, 2)
    assert len(context_of(repl)._processors) >= 3           # 先 /context 不能让这个会话丢了压缩器
    await repl.handle_slash("/context")
    out = capsys.readouterr().out
    assert "2 轮 · 4 条消息" in out and "最近一次请求" in out and "工具定义" in out


async def test_context_when_compression_disabled(env, capsys):
    repl, fake = env(enable_context_rails=False)
    assert repl.bundle.compression is None
    await chat(repl, fake, 1)
    await repl.handle_slash("/context")
    assert "自动压缩没有开启" in capsys.readouterr().out
    await repl.handle_slash("/compact")
    assert "上下文压缩没有开启" in capsys.readouterr().out


# ---------------------------------------------------------------- /compact
async def test_compact_with_instruction(env, capsys):
    repl, fake = env()
    await repl.handle_slash("/compact")
    assert "还没有对话内容" in capsys.readouterr().out

    await chat(repl, fake, 4)
    compressor = FakeCompressor()
    use_fake_compressor(repl, compressor)
    before = len(context_of(repl).get_messages())
    await repl.handle_slash("/compact 保留问题的编号")
    out = capsys.readouterr().out
    assert "正在压缩上下文" in out and "完成" in out, out
    assert compressor.prompts and all("保留问题的编号" in p for p in compressor.prompts)
    after = len(context_of(repl).get_messages())
    assert after < before

    fake.plan(("好的", []))
    await repl.run_interruptible("继续")
    assert fake.calls[-1]["n_msgs"] == 1 + after + 1          # 下一轮用的是压缩后的上下文
    notes = system_notes(repl)
    assert any(n.startswith("手动压缩上下文") for n in notes)


async def test_compact_reports_failure_reason(env, capsys):
    repl, fake = env()
    await chat(repl, fake, 4)
    use_fake_compressor(repl, FakeCompressor(error=RuntimeError("gateway said no")))
    before = len(context_of(repl).get_messages())
    await repl.handle_slash("/compact")
    out = capsys.readouterr().out
    assert "失败，上下文保持原样" in out and "RuntimeError: gateway said no" in out
    assert len(context_of(repl).get_messages()) == before


# ---------------------------------------------------------------- 自动压缩的提示
async def test_auto_compression_prints_one_line(env, capsys):
    repl, fake = env()
    repl.bundle.agent.update_model_context(model_name="fake", context_window_tokens=12000)
    await chat(repl, fake, 1, answer="好")
    use_fake_compressor(repl, FakeCompressor())
    await chat(repl, fake, 4)
    lines = [line for line in capsys.readouterr().out.splitlines() if "⟳" in line]
    assert lines and all("上下文已压缩：对话约" in line for line in lines), lines
    notes = system_notes(repl)
    assert any(n.startswith("上下文已压缩") for n in notes)


async def test_slow_compression_shows_progress_on_same_line(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "COMPRESSING_HINT_DELAY", 0.01)
    repl, fake = env()
    repl.bundle.agent.update_model_context(model_name="fake", context_window_tokens=12000)
    await chat(repl, fake, 1, answer="好")
    use_fake_compressor(repl, FakeCompressor(delay=0.2))
    await chat(repl, fake, 4)
    lines = [line for line in capsys.readouterr().out.splitlines() if "⟳" in line]
    assert lines and all("正在压缩上下文（对话约" in line and "… 完成，对话约" in line for line in lines), lines


async def test_auto_compression_failure_is_reported(env, capsys):
    repl, fake = env()
    repl.bundle.agent.update_model_context(model_name="fake", context_window_tokens=12000)
    await chat(repl, fake, 1, answer="好")
    use_fake_compressor(repl, FakeCompressor(error=RuntimeError("only stream=true is supported")))
    await chat(repl, fake, 4)
    out = capsys.readouterr().out
    assert "上下文压缩失败，上下文保持原样：RuntimeError: only stream=true is supported" in out


# ---------------------------------------------------------------- 模型报超长：压缩后重试
class Overflow(Exception):
    pass


@pytest.mark.parametrize("message", [
    "Error code: 400 - This model's maximum context length is 32768 tokens. However, you requested 40000 tokens",
    "调用失败：输入长度超过模型最大上下文 32768",
])
async def test_overflow_is_compressed_and_retried(env, capsys, message):
    repl, fake = env()
    await chat(repl, fake, 4)
    compressor = FakeCompressor()
    use_fake_compressor(repl, compressor)

    stream = fake.stream
    failures = [Overflow(message)]

    async def overflowing_stream(*args, **kwargs):
        if failures:
            fake.calls.append({"overflow": True, "n_msgs": len(kwargs.get("messages") or [])})
            raise failures.pop()
        async for chunk in stream(*args, **kwargs):
            yield chunk

    repl.bundle.agent.deep_config.model.stream = overflowing_stream
    fake.plan(("压缩后接着回答", []))
    await repl.run_interruptible("再问一个")
    out = capsys.readouterr().out
    assert "模型报上下文超长，已压缩上下文：" in out and "重试中" in out, out
    assert "压缩后接着回答" in out
    overflow_call, retry = fake.calls[-2], fake.calls[-1]
    assert overflow_call.get("overflow") and retry["n_msgs"] < overflow_call["n_msgs"]


def test_zh_overflow_detection():
    for text in ("输入长度超过模型最大上下文 32768", "上下文超长，请缩短输入", "prompt 长度超出限制", "Token数超过上限"):
        assert is_zh_context_overflow(Exception(text)), text
    for text in ("输出 token 超过限制", "max_tokens 参数超过上限", "连接超时", "鉴权失败", "请求频率超过限制"):
        assert not is_zh_context_overflow(Exception(text)), text
    try:
        try:
            raise ValueError("上下文长度超出模型限制")
        except ValueError as inner:
            raise RuntimeError("[181001] model call failed") from inner
    except RuntimeError as outer:
        assert is_zh_context_overflow(outer)                     # 在异常链里也能认出来


def test_fmt_tokens():
    assert [fmt_tokens(n) for n in (0, 999, 1000, 12345, 200000, 1050000)] == \
        ["0", "999", "1k", "12.3k", "200k", "1.05M"]


# ---------------------------------------------------------------- 重启后续聊
async def test_compact_survives_restart(tmp_path: Path, capsys):
    pytest.importorskip("aiosqlite")
    from test_resume import MoleProcess

    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    async with MoleProcess(proj, home) as first:
        for i in range(4):
            first.fake.plan((f"第 {i} 个回答：{LONG_A}", []))
            await first.repl.run_interruptible(f"第 {i} 个问题：{LONG_Q}")
        use_fake_compressor(first.repl, FakeCompressor())
        await first.repl.handle_slash("/compact")
        kept = len(context_of(first.repl).get_messages())
        sid = first.repl.session_id
    assert kept < 8

    async with MoleProcess(proj, home) as second:
        assert await second.repl.resume(sid)
        capsys.readouterr()
        await second.repl.handle_slash("/context")               # 从检查点读出来的已经是压缩后的
        assert f"· {kept} 条消息" in capsys.readouterr().out
        await second.repl.handle_slash("/compact")               # 这次启动后还没发过消息：压缩器还没装上
        assert "先接着聊一句" in capsys.readouterr().out
        second.fake.plan(("接着聊", []))
        await second.repl.run_interruptible("继续")
        assert second.streamed()[0]["n_msgs"] == 1 + kept + 1
        assert len(context_of(second.repl)._processors) >= 3     # 前面的 /context、/compact 没让这个会话丢了压缩器

"""会话历史：用 openjiuwen 自带的 SessionStore 记录，/history 列出当前项目的会话、按序号看详情。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from openjiuwen.core.runner import Runner

from mole_agent import cli
from mole_agent.agent import build_agent
from mole_agent.config import Settings
from mole_agent.history import HistoryRecorder, list_sessions, load_session, local_time, sessions_dir


def _settings(tmp_path: Path, name: str = "proj", **kw) -> Settings:
    proj = tmp_path / name
    proj.mkdir(exist_ok=True)
    return Settings(model="fake", api_key="k", api_base="http://127.0.0.1:9/v1",
                    project_dir=proj, home_dir=tmp_path / "home", **kw)


# ---------------------------------------------------------------- 存储
def test_recorder_writes_sdk_session_format(tmp_path: Path):
    settings = _settings(tmp_path)
    rec = HistoryRecorder(settings)
    rec.start("mole-aaaa0001", "deepseek/deepseek-flash")
    assert not list(rec.dir.glob("*.json"))                   # 空会话不留文件
    rec.user("看看这个仓库")
    rec.tool("bash", "ls", True, "executed", "Command: ls\nREADME.md\nsrc")
    rec.tool("bash", "sudo rm", False, "rejected_by_guard", "命令被安全规则拦截")
    rec.assistant("  仓库里有 README 和 src。  ")
    data = json.loads((rec.dir / "mole-aaaa0001.json").read_text(encoding="utf-8"))
    assert set(data) == {"session_id", "model", "created_at", "messages"}   # 就是 SDK 的 StoredSession
    from openjiuwen.harness.cli.storage import SessionStore

    assert SessionStore(store_dir=rec.dir).list_sessions()[0]["turns"] == 4  # SDK 自己也能读
    assert not list(rec.dir.glob("*.tmp"))                                   # 原子写入，没有残留临时文件
    assert [m["role"] for m in data["messages"]] == ["user", "tool", "tool", "assistant"]
    assert data["messages"][1]["content"] == "bash(ls) → 成功：README.md"   # 跳过回显的 Command 行
    assert "被拦截" in data["messages"][2]["content"]
    assert data["messages"][3]["content"] == "仓库里有 README 和 src。"
    assert (rec.dir / "project.txt").read_text(encoding="utf-8") == str(settings.project_dir.resolve())


def test_sessions_are_separated_by_project(tmp_path: Path):
    a, b = _settings(tmp_path, "a"), _settings(tmp_path, "b")
    assert sessions_dir(a) != sessions_dir(b) and sessions_dir(a).parent == sessions_dir(b).parent
    assert sessions_dir(a).name.startswith("a-")
    for settings, sid in ((a, "mole-a"), (b, "mole-b")):
        rec = HistoryRecorder(settings)
        rec.start(sid, "m")
        rec.user("hi")
    assert [s.id for s in list_sessions(sessions_dir(a))] == ["mole-a"]


def test_list_is_newest_first_with_title_and_turns(tmp_path: Path):
    settings = _settings(tmp_path)
    rec = HistoryRecorder(settings)
    for i, sid in enumerate(["mole-zzzz", "mole-aaaa", "mole-mmmm"]):  # 文件名顺序和时间顺序不同
        rec.start(sid, f"p/model-{i}")
        rec.user(f"第 {i} 个会话的   第一个\n问题" + "很长" * 50)
        rec.assistant("好的")
        rec.user("第二个问题")
        os.utime(rec.dir / f"{sid}.json", (1_000_000 + i, 1_000_000 + i))
    items = list_sessions(rec.dir)
    assert [s.id for s in items] == ["mole-mmmm", "mole-aaaa", "mole-zzzz"]
    assert items[0].user_turns == 2 and items[0].model == "p/model-2"
    assert items[0].title.startswith("第 2 个会话的 第一个 问题") and items[0].title.endswith("…")
    assert len(list_sessions(rec.dir, limit=2)) == 2


def test_broken_or_foreign_files_are_skipped(tmp_path: Path):
    settings = _settings(tmp_path)
    rec = HistoryRecorder(settings)
    rec.start("mole-good", "m")
    rec.user("hi")
    (rec.dir / "mole-bad.json").write_text("{not json", encoding="utf-8")
    (rec.dir / "other.json").write_text('{"x": 1}', encoding="utf-8")
    assert [s.id for s in list_sessions(rec.dir)] == ["mole-good"]
    assert load_session(rec.dir, "mole-bad") is None
    assert load_session(rec.dir, "../../etc/passwd") is None
    assert load_session(rec.dir, "nope") is None


def test_write_failure_does_not_break(tmp_path: Path, monkeypatch):
    rec = HistoryRecorder(_settings(tmp_path))
    rec.start("mole-x", "m")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(rec, "_save", boom)
    rec.user("hi")  # 不抛异常


def test_local_time_converts_utc():
    assert local_time("2026-09-24T02:30:00+00:00", "%Y") == "2026"
    assert local_time("garbage") == "garbage"
    assert local_time(time.time(), "%Y") == time.strftime("%Y")


# ---------------------------------------------------------------- REPL 端到端
@pytest.fixture()
async def repl_env(tmp_path: Path):
    from test_e2e_fake_llm import ScriptedModel

    settings = _settings(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=settings.project_dir, check=True)
    bundle = build_agent(settings)
    fake = ScriptedModel()
    model = bundle.agent.deep_config.model
    model.stream, model.invoke = fake.stream, fake.invoke
    repl = cli.Repl(settings, bundle)
    answers: list[str] = []

    async def fake_ask(_message: str) -> str:
        return answers.pop(0) if answers else ""

    repl._ask = fake_ask
    await Runner.start()
    try:
        yield settings, repl, fake, answers
    finally:
        await Runner.stop()


async def test_history_command_lists_and_shows_detail(repl_env, capsys):
    settings, repl, fake, answers = repl_env
    first_id = repl.session_id

    fake.plan(("先看看。", [("bash", {"command": "echo hi"})]), ("看完了，一切正常。", []))
    answers[:] = ["y"]                                            # 批准 bash
    await repl.run_interruptible("检查一下项目", label="检查一下项目")
    await repl.handle_slash("/new")
    fake.plan(("第二个会话的回答", []))
    await repl.run_interruptible("第二个问题")
    capsys.readouterr()

    answers[:] = ["2"]                                            # 列表里选第 2 个（更早的那个）
    await repl.handle_slash("/history")
    out = capsys.readouterr().out
    assert "历史会话" in out and "第二个问题" in out and "检查一下项目" in out
    assert out.index("第二个问题") < out.index("检查一下项目")      # 最新的在前
    assert "← 当前" in out
    assert first_id in out                                        # 详情标题里是被选中会话的 id
    assert "› " in out and "先看看。" in out and "看完了，一切正常。" in out
    assert "bash(echo hi) → 成功" in out

    await repl.handle_slash("/history 1")                         # 直接带序号
    out = capsys.readouterr().out
    assert "第二个会话的回答" in out and "（当前会话）" in out

    await repl.handle_slash(f"/history {first_id[:9]}")           # 用 id 前缀
    assert "看完了，一切正常。" in capsys.readouterr().out

    await repl.handle_slash("/history 9")
    assert "序号超出范围" in capsys.readouterr().out


async def test_history_records_slash_label_model_switch_and_errors(repl_env):
    from mole_agent.models import ModelChoice, ProviderConfig

    settings, repl, fake, answers = repl_env
    fake.plan(("检视完成", []))
    await repl.run_interruptible(cli.review_prompt("", True), label="/review")
    other = ProviderConfig(name="other", api_base="http://127.0.0.1:9/v2", api_key="k2", models=["m2"])
    repl.switch_to(ModelChoice(other, "m2"))

    async def broken_turn(_text: str) -> str:
        raise RuntimeError("boom")

    repl.run_turn = broken_turn
    await repl.run_interruptible("会出错")
    session = load_session(repl.history.dir, repl.session_id)
    roles = [(m.role, m.content) for m in session.messages]
    assert roles[0] == ("user", "/review")                        # 记用户敲的命令，不是展开后的长提示词
    assert ("system", "切换模型：other/m2") in roles
    assert roles[-2] == ("user", "会出错") and roles[-1][0] == "system" and "boom" in roles[-1][1]


async def test_interrupted_turn_keeps_partial_output(repl_env):
    import asyncio

    settings, repl, fake, answers = repl_env

    async def slow_stream(*args, **kwargs):
        from openjiuwen.core.foundation.llm import AssistantMessageChunk

        yield AssistantMessageChunk(content="写到一半", tool_calls=[])
        await asyncio.sleep(30)
        yield AssistantMessageChunk(content="不会出现", tool_calls=[], finish_reason="stop")

    repl.bundle.agent.deep_config.model.stream = slow_stream
    task = asyncio.create_task(repl.run_interruptible("长任务"))
    for _ in range(100):
        await asyncio.sleep(0.05)
        session = load_session(repl.history.dir, repl.session_id)
        if session and any(m.role == "user" for m in session.messages):
            break
    await asyncio.sleep(0.3)
    task.cancel()   # 效果和 Ctrl+C 一样：当前这一轮被取消
    try:
        await task
    except asyncio.CancelledError:
        pass
    session = load_session(repl.history.dir, repl.session_id)
    contents = [m.content for m in session.messages]
    assert contents[0] == "长任务"
    assert any("写到一半" in c for c in contents)                  # 中断前已经输出的部分也留下了
    assert contents[-1] == "已中断"


def test_history_can_be_disabled(tmp_path: Path):
    settings = _settings(tmp_path, save_history=False)
    repl = cli.Repl(settings, bundle=None)
    assert repl.history is None
    assert not (settings.home_dir / "sessions").exists()


# ---------------------------------------------------------------- 搜索
def _seed(settings: Settings) -> Path:
    rec = HistoryRecorder(settings)
    data = [
        ("mole-s0000001", [("user", "你好"), ("assistant", "你好！我是 Mole，有什么可以帮你？")]),
        ("mole-s0000002", [("user", "帮我看看 README"), ("tool", "read_file(README.md) → 成功：# Mole"),
                           ("assistant", "README 介绍了安装步骤。")]),
        ("mole-s0000003", [("user", "修一下登录的 bug"), ("assistant", "已经修好，顺便说声你好。")]),
        ("mole-s0000004", [("user", "端口 8080 被占用了"), ("assistant", "换成 9090 吧")]),
    ]
    for i, (sid, messages) in enumerate(data):
        rec.start(sid, "m")
        for role, content in messages:
            getattr(rec, role)(content) if role != "tool" else rec._add("tool", content)
        os.utime(rec.dir / f"{sid}.json", (1_000_000 + i, 1_000_000 + i))
    return rec.dir


def test_search_matches_title_and_content(tmp_path: Path):
    from mole_agent.history import search_sessions

    d = _seed(_settings(tmp_path))
    ids = lambda q: [h.session.id for h in search_sessions(d, q)]  # noqa: E731
    assert ids("你好") == ["mole-s0000003", "mole-s0000001"]            # 标题里有、回复里有，都找出来，最近的在前
    assert ids("readme") == ["mole-s0000002"]                           # 不区分大小写
    assert ids("# mole") == ["mole-s0000002"]                           # 工具摘要里也搜（多个词：# 和 mole 都要出现）
    assert ids("登录 你好") == ["mole-s0000003"]                        # 多个关键词要都出现
    assert ids("不存在的词") == [] and ids("   ") == []
    assert ids("mole") == ["mole-s0000002", "mole-s0000001"]            # 普通关键词，不当成会话 id


def test_search_snippet_and_keyword_parsing(tmp_path: Path):
    from mole_agent.history import search_keywords, search_sessions

    assert search_keywords("登录 你好") == ["登录", "你好"]
    assert search_keywords('"8080"') == ["8080"] and search_keywords("「登录 bug」") == ["登录 bug"]
    d = _seed(_settings(tmp_path))
    hit = next(h for h in search_sessions(d, "你好") if h.session.id == "mole-s0000003")
    assert hit.snippet.startswith("回复：") and "你好" in hit.snippet    # 片段显示命中的那条消息
    assert [h.session.id for h in search_sessions(d, '"8080"')] == ["mole-s0000004"]


async def test_history_keyword_search_after_new_session(repl_env, capsys):
    """复现：输入「你好」→ 模型回答 → /new → /history 你好，要能找到刚才的会话。"""
    settings, repl, fake, answers = repl_env
    fake.plan(("你好！有什么可以帮你？", []))
    await repl.run_interruptible("你好")
    first_id = repl.session_id
    await repl.handle_slash("/new")
    capsys.readouterr()

    answers[:] = ["1"]
    await repl.handle_slash("/history 你好")
    out = capsys.readouterr().out
    assert "找不到" not in out and "搜索「你好」" in out and "找到 1 个会话" in out
    assert first_id in out and "有什么可以帮你" in out                 # 选 1 后显示了详情

    await repl.handle_slash("/history 帮你")                            # 只在回复里出现的词也能搜到
    assert "找到 1 个会话" in capsys.readouterr().out

    await repl.handle_slash("/history 完全不相关")
    assert "没有找到包含「完全不相关」的会话" in capsys.readouterr().out

    answers[:] = ["你好", "1"]                                          # 不带参数时，也可以在提示里输入关键词
    await repl.handle_slash("/history")
    assert "搜索「你好」" in capsys.readouterr().out

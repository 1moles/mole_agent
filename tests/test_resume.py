"""续聊：SDK 的 PersistenceCheckpointer（SQLite）保存对话上下文，重启后 /resume、mole -c 接着聊。

每个用例里「一次 mole 进程」= 新建 agent + 打开检查点 + Runner.start ... Runner.stop + 关闭检查点，
两次之间只共享磁盘上的 ~/.mole-agent（检查点数据库和会话历史文件）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")
pytest.importorskip("sqlalchemy.ext.asyncio")

from openjiuwen.core.runner import Runner  # noqa: E402
from openjiuwen.core.session.checkpointer import CheckpointerFactory  # noqa: E402

from mole_agent import cli  # noqa: E402
from mole_agent.agent import build_agent  # noqa: E402
from mole_agent.config import Settings  # noqa: E402
from mole_agent.history import CHECKPOINT_DB, load_session, open_context_checkpoint  # noqa: E402


@pytest.fixture()
def workdir(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    return proj, tmp_path / "home"


class MoleProcess:
    """模拟一次 mole 启动到退出。"""

    def __init__(self, proj: Path, home: Path, answers: list[str] | None = None) -> None:
        from test_e2e_fake_llm import ScriptedModel

        self.settings = Settings(model="fake", api_key="k", api_base="http://127.0.0.1:9/v1",
                                 project_dir=proj, home_dir=home)
        self.bundle = build_agent(self.settings)
        self.fake = ScriptedModel()
        model = self.bundle.agent.deep_config.model
        model.stream, model.invoke = self.fake.stream, self.fake.invoke
        self.repl = cli.Repl(self.settings, self.bundle)
        self.answers = list(answers or [])
        self.asked: list[str] = []

        async def fake_ask(message: str) -> str:
            self.asked.append(message)
            return self.answers.pop(0) if self.answers else ""

        self.repl._ask = fake_ask
        self.checkpoint = None

    async def __aenter__(self) -> "MoleProcess":
        self.checkpoint, reason = await open_context_checkpoint(self.settings)
        assert self.checkpoint is not None, reason
        self.repl.checkpoint = self.checkpoint
        await Runner.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await Runner.stop()
        await self.checkpoint.close()

    def streamed(self) -> list[dict]:
        return [c for c in self.fake.calls if not c.get("invoke")]


async def test_resume_restores_context_after_restart(workdir, capsys):
    proj, home = workdir
    default_before = CheckpointerFactory.get_checkpointer()

    async with MoleProcess(proj, home) as first:
        first.fake.plan(("好的，记住了：你叫小明。", []))
        await first.repl.run_interruptible("我叫小明，记住")
        first_id = first.repl.session_id
    assert CheckpointerFactory.get_checkpointer() is default_before   # 退出时换回 SDK 默认的检查点
    assert (home / CHECKPOINT_DB).is_file()

    async with MoleProcess(proj, home, answers=["1"]) as second:
        await second.repl.handle_slash("/resume")                   # 列表里第 1 个就是上次的会话
        assert second.repl.session_id == first_id
        out = capsys.readouterr().out
        assert "已回到会话" in out and "上次问：我叫小明，记住" in out and "对话上下文已恢复" in out

        second.fake.plan(("你叫小明。", []))
        await second.repl.run_interruptible("我叫什么？")
        call = second.streamed()[0]
        assert call["n_msgs"] == 4                                   # system + 上次一问一答 + 这次的问题

    session = load_session(second.repl.history.dir, first_id)       # 接着写进同一个历史文件
    assert [(m.role, m.content) for m in session.messages if m.role in {"user", "assistant"}] == [
        ("user", "我叫小明，记住"), ("assistant", "好的，记住了：你叫小明。"),
        ("user", "我叫什么？"), ("assistant", "你叫小明。"),
    ]
    assert any(m.role == "system" and m.content.startswith("继续会话") for m in session.messages)


async def test_continue_latest_and_new_session_is_empty(workdir):
    proj, home = workdir
    async with MoleProcess(proj, home) as first:
        first.fake.plan(("第一个会话", []))
        await first.repl.run_interruptible("会话一")
        await first.repl.handle_slash("/new")
        first.fake.plan(("第二个会话", []))
        await first.repl.run_interruptible("会话二")
        latest = first.repl.session_id

    async with MoleProcess(proj, home) as second:                   # mole -c
        assert await second.repl.continue_latest()
        assert second.repl.session_id == latest
        second.fake.plan(("接着第二个会话", []))
        await second.repl.run_interruptible("继续")
        assert second.streamed()[0]["n_msgs"] == 4

    async with MoleProcess(proj, home) as third:                    # 不续聊：新会话从空上下文开始
        third.fake.plan(("新会话", []))
        await third.repl.run_interruptible("你好")
        assert third.streamed()[0]["n_msgs"] == 2


async def test_pending_approval_is_asked_again_after_resume(workdir):
    """上次停在等确认的操作上（比如 Ctrl+C 或直接关了终端），续聊后先重新问，不会自己执行。"""
    import asyncio

    proj, home = workdir
    async with MoleProcess(proj, home) as first:
        async def cancel(_message: str) -> str:
            raise asyncio.CancelledError  # 在确认提示处按了 Ctrl+C

        first.repl._ask = cancel
        first.fake.plan(("", [("bash", {"command": "echo a > a.txt"})]))
        await first.repl.run_interruptible("写个文件")
        sid = first.repl.session_id
    assert not (proj / "a.txt").exists()

    async with MoleProcess(proj, home, answers=["n"]) as second:
        assert await second.repl.resume(sid)
        second.fake.plan(("好的，不写了。", []))
        await second.repl.run_interruptible("算了")
        assert any("允许" in q for q in second.asked)                 # 重新问了
    assert not (proj / "a.txt").exists()                              # 拒绝后没有执行


async def test_resume_refuses_sessions_without_checkpoint(workdir, capsys):
    from mole_agent.history import HistoryRecorder

    proj, home = workdir
    async with MoleProcess(proj, home) as proc:
        old = HistoryRecorder(proc.settings)                          # 开启续聊之前留下的历史：只有 JSON，没有检查点
        old.start("mole-old00001", "m")
        old.user("很久以前的问题")
        assert not await proc.repl.resume("mole-old00001")
        assert "没有保存对话上下文" in capsys.readouterr().out
        assert proc.repl.session_id != "mole-old00001"
        assert not await proc.repl.continue_latest()                  # 没有能续聊的会话
        assert "没有可以继续的会话" in capsys.readouterr().out


async def test_history_detail_offers_resume(workdir, capsys):
    proj, home = workdir
    async with MoleProcess(proj, home) as first:
        first.fake.plan(("第一次的回答", []))
        await first.repl.run_interruptible("第一次的问题")
        sid = first.repl.session_id

    async with MoleProcess(proj, home, answers=["1", "r"]) as second:
        await second.repl.handle_slash("/history")                   # 选 1 看详情，再按 r
        assert second.repl.session_id == sid
        assert "第一次的回答" in capsys.readouterr().out


async def test_always_allow_does_not_follow_resumed_session(workdir):
    proj, home = workdir
    async with MoleProcess(proj, home) as first:
        first.fake.plan(("好", []))
        await first.repl.run_interruptible("hi")
        sid = first.repl.session_id
        await first.repl.handle_slash("/new")
        first.bundle.approval.always_allow.add("bash")
        assert await first.repl.resume(sid)
        assert first.bundle.approval.always_allow == set()


async def test_missing_aiosqlite_degrades_gracefully(workdir, monkeypatch, capsys):
    import builtins

    proj, home = workdir
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "aiosqlite":
            raise ImportError("No module named 'aiosqlite'", name="aiosqlite")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    settings = Settings(model="fake", api_key="k", api_base="http://x/v1", project_dir=proj, home_dir=home)
    checkpoint, reason = await open_context_checkpoint(settings)
    assert checkpoint is None and "aiosqlite" in reason and "pip install" in reason
    monkeypatch.setattr(builtins, "__import__", real_import)

    repl = cli.Repl(settings, bundle=None)
    repl.checkpoint_error = reason
    assert not await repl.resume("mole-x")
    assert "续聊不可用" in capsys.readouterr().out


async def test_resume_by_keyword(workdir):
    proj, home = workdir
    async with MoleProcess(proj, home) as first:
        first.fake.plan(("在看登录模块", []))
        await first.repl.run_interruptible("帮我看看登录模块")
        target = first.repl.session_id
        await first.repl.handle_slash("/new")
        first.fake.plan(("好", []))
        await first.repl.run_interruptible("别的事情")

    async with MoleProcess(proj, home, answers=["1"]) as second:
        await second.repl.handle_slash("/resume 登录")                 # 关键词搜索后选 1
        assert second.repl.session_id == target

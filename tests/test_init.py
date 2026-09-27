from types import SimpleNamespace
from enum import Enum

import pytest


@pytest.mark.parametrize("as_object", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_controller_failure_uses_event_type(as_object, failed):
    from mole_agent.cli import StreamRenderer

    class EventType(Enum):
        TASK_FAILED = "task_failed"
        TASK_COMPLETED = "task_completed"

    event = EventType.TASK_FAILED if failed else EventType.TASK_COMPLETED
    # 成功事件的正文也可能讨论 task_failed，不能据此改变执行结果。
    payload = {"type": event.value, "data": [{"text": "已验证 task_failed 的错误处理"}]}
    if as_object:
        payload = SimpleNamespace(type=event, data=payload["data"])
    renderer = StreamRenderer()
    renderer.feed({"type": "controller_output", "payload": payload})
    assert renderer.failed is failed


def test_init_preserves_crlf_bytes_and_unchanged_file(tmp_path):
    from mole_agent.commands.init import InitDraft

    target = tmp_path / "AGENTS.md"
    original = "# 规则\r\n保留原内容。\r\n".encode("utf-8")
    target.write_bytes(original)
    before = target.stat().st_mtime_ns
    with InitDraft(tmp_path, tmp_path / "workspace") as draft:
        draft.path.write_bytes(original)
        assert draft.commit() == "unchanged"
    assert target.read_bytes() == original
    assert target.stat().st_mtime_ns == before

    updated = original + "新增约定。\r\n".encode("utf-8")
    with InitDraft(tmp_path, tmp_path / "workspace") as draft:
        draft.path.write_bytes(updated)
        assert draft.commit() == "updated"
    assert target.read_bytes() == updated


def test_target_and_existing_load_behavior(tmp_path):
    from mole_agent.instructions import init_target, load_project_memory

    assert init_target(tmp_path) == tmp_path / "AGENTS.md"
    (tmp_path / "AGENTS.md").write_text("fallback")
    (tmp_path / "MOLE.md").write_text("preferred")
    assert init_target(tmp_path) == tmp_path / "MOLE.md"
    assert load_project_memory(tmp_path) == ("MOLE.md", "preferred")


def test_init_draft_commit_and_conflict(tmp_path):
    from mole_agent.commands.init import InitDraft

    project = tmp_path / "project"
    project.mkdir()
    target = project / "AGENTS.md"
    with InitDraft(project, tmp_path / "workspace") as draft:
        draft.path.write_text("# Rules\nUse uv.\n")
        assert draft.commit() == "created"
    assert target.read_text() == "# Rules\nUse uv.\n"
    assert not draft.path.exists()
    with InitDraft(project, tmp_path / "workspace") as draft:
        draft.path.write_text("generated")
        target.write_text("concurrent human edit")
        with pytest.raises(ValueError, match="修改"):
            draft.commit()
    assert target.read_text() == "concurrent human edit"


def test_init_requires_nonempty_output(tmp_path):
    from mole_agent.commands.init import InitDraft

    with InitDraft(tmp_path, tmp_path / "workspace") as draft:
        with pytest.raises(ValueError, match="草稿"):
            draft.commit()
        draft.path.write_text(" \n")
        with pytest.raises(ValueError, match="为空"):
            draft.commit()
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_detects_shadowing_and_rejects_symlinks(tmp_path):
    from mole_agent.commands.init import InitDraft

    with InitDraft(tmp_path, tmp_path / "workspace") as draft:
        draft.path.write_text("generated")
        (tmp_path / "MOLE.md").write_text("new higher priority rules")
        with pytest.raises(ValueError, match="修改"):
            draft.commit()
    assert not (tmp_path / "AGENTS.md").exists()
    (tmp_path / "MOLE.md").unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("keep")
    (tmp_path / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        InitDraft(tmp_path, tmp_path / "workspace")
    assert outside.read_text() == "keep"


def test_existing_rules_unchanged_and_mode_preserved(tmp_path):
    from mole_agent.commands.init import InitDraft

    target = tmp_path / "CLAUDE.md"
    target.write_text("manual rules")
    target.chmod(0o600)
    before = target.stat().st_mtime_ns
    with InitDraft(tmp_path, tmp_path / "workspace") as draft:
        draft.path.write_text("manual rules")
        assert draft.commit() == "unchanged"
    assert target.stat().st_mtime_ns == before
    with InitDraft(tmp_path, tmp_path / "workspace") as draft:
        draft.path.write_text("manual rules\nadditional rule")
        assert draft.commit() == "updated"
    assert target.stat().st_mode & 0o777 == 0o600


def test_instruction_read_failure_and_limit(tmp_path, monkeypatch):
    from pathlib import Path
    from mole_agent.instructions import load_project_memory, PROJECT_MEMORY_MAX_CHARS

    (tmp_path / "MOLE.md").write_text("unreadable")
    (tmp_path / "AGENTS.md").write_text("a" * (PROJECT_MEMORY_MAX_CHARS + 1))
    read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "MOLE.md":
            raise PermissionError("denied")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    name, content = load_project_memory(tmp_path)
    assert name == "AGENTS.md" and content.endswith("...(已截断)")


@pytest.mark.asyncio
async def test_init_command_completion_and_cancel(tmp_path, monkeypatch):
    from mole_agent import cli
    from mole_agent.commands import AgentPrompt
    from mole_agent.config import Settings

    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(project_dir=project, home_dir=tmp_path / "home", save_history=False)
    repl = cli.Repl(settings, SimpleNamespace(subagents={}))
    refreshed = []
    monkeypatch.setattr(cli, "refresh_project_instructions", lambda *args: refreshed.append(True))
    command = await repl.handle_slash("/init 重点测试")
    assert isinstance(command, AgentPrompt)
    assert "重点测试" in command.text
    drafts = list(settings.workspace_dir.glob("init-*/draft.md"))
    assert len(drafts) == 0  # Model must create the output, not inherit a fake success.
    paths = list(settings.workspace_dir.glob("init-*"))
    (paths[0] / "draft.md").write_text("# Rules\nUse pytest.")
    result = await command.on_finish(True)
    assert "已创建" in result.text and refreshed == [True]
    assert (project / "AGENTS.md").read_text() == "# Rules\nUse pytest."
    command = await repl.handle_slash("/init")
    paths = list(settings.workspace_dir.glob("init-*"))
    (paths[0] / "draft.md").write_text("partial")
    result = await command.on_finish(False)
    assert "未写回" in result.text
    assert (project / "AGENTS.md").read_text() == "# Rules\nUse pytest."
    assert not list(settings.workspace_dir.glob("init-*"))


@pytest.mark.asyncio
async def test_init_missing_draft_and_refresh_failure(tmp_path, monkeypatch):
    from mole_agent import cli
    from mole_agent.config import Settings

    settings = Settings(project_dir=tmp_path, home_dir=tmp_path / "home", save_history=False)
    repl = cli.Repl(settings, SimpleNamespace(subagents={}))
    command = await repl.handle_slash("/init")
    result = await command.on_finish(True)
    assert "未生成" in result.text and not (tmp_path / "AGENTS.md").exists()
    assert not list(settings.workspace_dir.glob("init-*"))

    def fail(*args):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(cli, "refresh_project_instructions", fail)
    command = await repl.handle_slash("/init")
    (next(settings.workspace_dir.glob("init-*")) / "draft.md").write_text("valid rules")
    result = await command.on_finish(True)
    assert "已创建" in result.text and "刷新失败" in result.text
    assert (tmp_path / "AGENTS.md").read_text() == "valid rules"


@pytest.mark.asyncio
async def test_prompt_completion_hook_on_failure(tmp_path):
    from mole_agent.cli import Repl
    from mole_agent.commands import AgentPrompt
    from mole_agent.config import Settings

    repl = Repl(Settings(home_dir=tmp_path, save_history=False), SimpleNamespace())
    outcomes = []

    async def finish(ok):
        outcomes.append(ok)

    async def fail(text):
        raise RuntimeError("test failure")

    repl.run_turn = fail
    await repl.run_interruptible(AgentPrompt("test", on_finish=finish))
    assert outcomes == [False]


@pytest.mark.asyncio
async def test_failed_stream_does_not_commit_draft(tmp_path, monkeypatch):
    from mole_agent import cli
    from mole_agent.config import Settings
    from openjiuwen.core.runner import Runner

    settings = Settings(project_dir=tmp_path, home_dir=tmp_path / 'home', save_history=False)
    activity = SimpleNamespace(subscribe=lambda handler: lambda: None)
    repl = cli.Repl(settings, SimpleNamespace(subagents={}, agent=object(), activity=activity, compression=None))
    target = tmp_path / 'AGENTS.md'
    target.write_text('original')
    command = await repl.handle_slash('/init')
    (next(settings.workspace_dir.glob('init-*')) / 'draft.md').write_text('partial draft')
    refreshed = []
    monkeypatch.setattr(cli, 'refresh_project_instructions', lambda *args: refreshed.append(True))
    streamed = []

    async def failed_stream(*args, **kwargs):
        streamed.append(True)
        yield {'type': 'controller_output', 'payload': {'type': 'task_failed', 'error': 'model failed'}}
        yield {'type': 'answer', 'payload': {'output': '', 'result_type': ''}}

    monkeypatch.setattr(Runner, 'run_agent_streaming', failed_stream)
    await repl.run_interruptible(command)
    assert streamed == [True]
    assert target.read_text() == 'original'
    assert not refreshed
    assert not list(settings.workspace_dir.glob('init-*'))

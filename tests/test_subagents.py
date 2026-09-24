"""子 agent：只读 shell 守卫、git_changes 的提交/分支范围、自动发现与配置、端到端派活。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from openjiuwen.core.runner import Runner

from mole_agent import cli
from mole_agent.agent import build_agent, build_model
from mole_agent.config import Settings
from mole_agent.models import ModelCatalog, ProviderConfig
from mole_agent.rails import ActivityFeed, ReadOnlyShellRail, TokenUsageRail, read_only_violation
from mole_agent.subagents import SubagentEnv, build_subagents, discover_subagent_modules
from mole_agent.tools.git_changes import changes

REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(tmp_path: Path, **kw) -> Settings:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Settings(model="fake", api_key="k", api_base="http://127.0.0.1:9/v1",
                    project_dir=proj, home_dir=tmp_path / "home", **kw)


def _env(settings: Settings, **kw) -> SubagentEnv:
    return SubagentEnv(settings=settings, build_model=build_model, **kw)


def _rail_types(config) -> list[str]:
    return [type(r).__name__ for r in config.rails]


# ---------------------------------------------------------------- 只读 shell
@pytest.mark.parametrize("cmd", [
    "ls -la", "cat a.py | head -20", "grep -rn foo src 2>/dev/null", "rg -n 'def run' mole_agent",
    "find . -name '*.py'", "wc -l a.py b.py", "git status --short", "git log --oneline -5",
    "git diff main...HEAD --stat", "git -C sub show HEAD~1", "git branch -a", "git blame -L 1,20 a.py",
    "sort a.txt", "diff a.py b.py && echo same",
])
def test_read_only_commands_allowed(cmd: str):
    assert read_only_violation(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "echo x > out.txt", "cat a >> b", "rm -rf build", "mv a b", "touch x", "python -c 'print(1)'",
    "pytest -q", "pip install foo", "git commit -m x", "git checkout -- a.py", "git push",
    "git branch -D old", "git -c core.pager=sh log", "git diff --output=patch.txt",
    "find . -name '*.pyc' -delete", "find . -exec rm {} ;", "sort -o out.txt a.txt",
    "ls $(whoami)", "ls `pwd`", "FOO=1 ls", "sed -i s/a/b/ x.py", "",
])
def test_non_read_only_commands_rejected(cmd: str):
    assert read_only_violation(cmd), cmd


async def test_read_only_shell_rail_is_an_interrupt_rail():
    rail = ReadOnlyShellRail()
    assert rail.priority >= 95  # 必须先于任何可能执行命令的环节


# ---------------------------------------------------------------- git_changes：提交 / 分支
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_git_changes_commit_and_base(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "change x to two")

    out = changes(repo, commit="HEAD")
    assert "change x to two" in out and "+x = 2" in out

    out = changes(repo, base="main")
    assert "change x to two" in out and "+x = 2" in out and "-x = 1" in out

    assert "找不到提交" in changes(repo, commit="deadbeef")
    for bad in ("--output=/tmp/x", "a b", "HEAD;rm -rf /"):
        assert "不是合法的 git 引用" in changes(repo, commit=bad)
        assert "不是合法的 git 引用" in changes(repo, base=bad)


# ---------------------------------------------------------------- 发现与配置
def test_discovers_every_subagent_file():
    import mole_agent.subagents as pkg

    files = sorted(p.stem for p in Path(pkg.__path__[0]).glob("*.py") if not p.stem.startswith("_"))
    assert [m.__name__.rsplit(".", 1)[-1] for m in discover_subagent_modules()] == files
    assert {"explore_agent", "code_reviewer"} <= set(files)


def test_file_name_equals_subagent_name(tmp_path: Path):
    configs = build_subagents(_env(_settings(tmp_path)))
    files = [m.__name__.rsplit(".", 1)[-1] for m in discover_subagent_modules()]
    assert [c.agent_card.name for c in configs] == files


def test_explore_agent_uses_sdk_builtin(tmp_path: Path):
    from openjiuwen.harness.subagents.explore_agent import (
        DEFAULT_EXPLORE_AGENT_DESCRIPTION,
        DEFAULT_EXPLORE_AGENT_SYSTEM_PROMPT,
    )

    settings = _settings(tmp_path)
    usage = TokenUsageRail()
    configs = {c.agent_card.name: c for c in build_subagents(_env(settings, usage_rail=usage))}
    explore = configs["explore_agent"]
    assert explore.system_prompt == DEFAULT_EXPLORE_AGENT_SYSTEM_PROMPT["cn"]      # 提示词、描述都是 SDK 自带的
    assert explore.agent_card.description == DEFAULT_EXPLORE_AGENT_DESCRIPTION["cn"]
    assert explore.model is None                                                  # 跟随主 agent 当前模型
    assert explore.workspace is not None                                          # 有 workspace，SDK 才会用 sys_operation
    assert usage in explore.rails
    assert {"SysOperationRail", "ReadOnlyShellRail", "ToolTraceRail"} <= set(_rail_types(explore))


def test_subagents_are_read_only(tmp_path: Path):
    for config in build_subagents(_env(_settings(tmp_path))):
        types = _rail_types(config)
        assert not {"ApprovalRail", "AskUserRail", "QuestionRail"} & set(types), config.agent_card.name
        sysop = next(r for r in config.rails if type(r).__name__ == "SysOperationRail")
        assert sysop._read_only is True  # noqa: SLF001 —— 测试里检查 SDK rail 的配置
        assert {t.card.name for t in config.tools} == {"git_changes", "project_overview"}


def test_code_reviewer_config(tmp_path: Path):
    settings = _settings(tmp_path)
    reviewer = {c.agent_card.name: c for c in build_subagents(_env(settings))}["code_reviewer"]
    assert "SkillUseRail" in _rail_types(reviewer)                 # 能读项目里的 code-review 技能
    assert "代码检视子代理" in reviewer.system_prompt
    assert "task_description" in reviewer.agent_card.description   # 告诉主 agent 怎么派活
    en = {c.agent_card.name: c for c in build_subagents(_env(_settings(tmp_path, language="en")))}["code_reviewer"]
    assert "code review subagent" in en.system_prompt


def test_rails_are_forked_per_subagent_instance(tmp_path: Path):
    usage = TokenUsageRail()
    reviewer = {c.agent_card.name: c for c in build_subagents(_env(_settings(tmp_path), usage_rail=usage))}["code_reviewer"]
    for rail in reviewer.rails:
        fork = getattr(rail, "fork_for_agent", None)
        if rail is usage:
            assert fork is None  # token 统计故意共用
            continue
        assert callable(fork)
        copy = fork()
        assert type(copy) is type(rail) and copy is not rail
        assert callable(getattr(copy, "fork_for_agent", None))


def test_subagent_model_from_models_toml(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOLE_TEST_SUB_KEY", "sk-sub")
    settings = _settings(tmp_path)
    settings.catalog = ModelCatalog(
        providers={
            "strong": ProviderConfig(name="strong", api_base="http://127.0.0.1:9/v1",
                                     api_key_env="MOLE_TEST_SUB_KEY", models=["big-model"]),
            "nokey": ProviderConfig(name="nokey", api_base="http://127.0.0.1:9/v1",
                                    api_key_env="MOLE_TEST_UNSET_KEY", models=["m"]),
        },
        subagent_models={"code_reviewer": "strong/big-model", "explore_agent": "nokey/m"},
    )
    env = _env(settings)
    configs = {c.agent_card.name: c for c in build_subagents(env)}
    assert configs["code_reviewer"].model.model_config.model_name == "big-model"
    assert configs["explore_agent"].model is None                   # 没配 key：回落到主模型并给出警告
    assert any("explore_agent" in w and "MOLE_TEST_UNSET_KEY" in w for w in env.warnings)
    assert env.model_label("code_reviewer") == "strong/big-model"
    assert env.model_label("explore_agent") == "跟随主模型"
    assert settings.model == "fake"                                 # 主 agent 的配置不受影响


def test_models_toml_subagents_table(tmp_path: Path):
    from mole_agent.models import load_catalog

    path = tmp_path / "models.toml"
    path.write_text(
        'default = "p/a"\n[subagents]\ncode_reviewer = "p/b"\n'
        '[providers.p]\napi_base = "http://x/v1"\napi_key = "k"\nmodels = ["a", "b"]\n',
        encoding="utf-8",
    )
    catalog = load_catalog([path])
    assert catalog.subagent_models == {"code_reviewer": "p/b"}
    assert catalog.default == "p/a" and catalog.providers["p"].models == ["a", "b"]


def test_build_agent_registers_subagents(tmp_path: Path):
    from mole_agent.prompts import build_system_prompt

    settings = _settings(tmp_path)
    bundle = build_agent(settings)
    assert bundle.subagents == {"code_reviewer": "跟随主模型", "explore_agent": "跟随主模型"}
    assert sorted(s.agent_card.name for s in bundle.agent.deep_config.subagents) == ["code_reviewer", "explore_agent"]
    prompt = build_system_prompt(settings, subagents=list(bundle.subagents))
    assert "## 子代理（task_tool）" in prompt and "code_reviewer" in prompt
    assert "子代理" not in build_system_prompt(settings)


def test_subagents_can_be_disabled(tmp_path: Path):
    bundle = build_agent(_settings(tmp_path, enable_subagents=False))
    assert bundle.subagents == {}
    assert not bundle.agent.deep_config.subagents


def test_read_only_tool_filter(tmp_path: Path, monkeypatch):
    import types

    import mole_agent.tools as tools_pkg
    from mole_agent.tools import git_changes

    writer = types.ModuleType("mole_agent.tools.writer")
    writer.create = git_changes.create  # 没声明 READ_ONLY：只给主 agent
    monkeypatch.setattr(tools_pkg, "discover_tool_modules", lambda: [git_changes, writer])
    settings = _settings(tmp_path)
    assert len(tools_pkg.build_custom_tools(settings, read_only=True)) == 1
    with pytest.raises(tools_pkg.ToolLoadError, match="重复"):  # 主 agent 两个都要（这里同名所以报重复）
        tools_pkg.build_custom_tools(settings)


def test_review_prompt_delegates_to_code_reviewer():
    assert "code_reviewer" in cli.review_prompt("", True)
    assert "检视当前未提交的改动" in cli.review_prompt("", True)
    assert "提交 a1b2c3d" in cli.review_prompt("提交 a1b2c3d", True)
    fallback = cli.review_prompt("重点看安全", False)
    assert "code_reviewer" not in fallback and "补充要求：重点看安全" in fallback


def test_activity_feed_isolates_listener_errors():
    feed, got = ActivityFeed(), []
    unsubscribe = feed.subscribe(got.append)
    feed.subscribe(lambda e: 1 / 0)
    feed.emit({"x": 1})
    unsubscribe()
    feed.emit({"x": 2})
    assert got == [{"x": 1}]


# ---------------------------------------------------------------- 端到端：主 agent → task_tool → code_reviewer
async def test_main_agent_delegates_review(tmp_path: Path):
    from test_e2e_fake_llm import ScriptedModel

    settings = _settings(tmp_path)
    proj = settings.project_dir
    shutil.copytree(REPO_ROOT / ".mole", proj / ".mole")
    (proj / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(proj, "init", "-q")
    _git(proj, "add", ".")
    _git(proj, "commit", "-q", "-m", "init")
    (proj / "app.py").write_text("print('hello')\n", encoding="utf-8")

    bundle = build_agent(settings)
    fake = ScriptedModel()
    model = bundle.agent.deep_config.model
    model.stream, model.invoke = fake.stream, fake.invoke   # 子 agent 没单独配模型，用的是同一个对象
    repl = cli.Repl(settings, bundle)
    asked: list[str] = []

    async def fake_ask(message: str) -> str:
        asked.append(message)
        return "y"

    repl._ask = fake_ask
    seen: list[dict] = []
    bundle.activity.subscribe(seen.append)

    fake.plan(
        # 主 agent：派给 code_reviewer
        ("", [("task_tool", {"subagent_type": "code_reviewer", "task_description": "代码检视。用户的要求：检视当前未提交的改动"})]),
        # code_reviewer：读技能、取改动、只读命令放行、写命令被拒、没有写工具
        ("", [("skill_tool", {"skill_name": "code-review"})]),
        ("", [("git_changes", {})]),
        ("", [("bash", {"command": "git status --short"})]),
        ("", [("bash", {"command": "echo hacked > pwned.txt"})]),
        ("", [("write_file", {"file_path": "pwned2.txt", "content": "x"})]),
        ("## 结论\n建议合入 —— 只改了一行输出", []),
        # 主 agent：转述报告
        ("检视结果：建议合入。", []),
    )
    await Runner.start()
    try:
        text = await repl.run_turn(cli.review_prompt("", True))
    finally:
        await Runner.stop()

    assert "建议合入" in text
    assert not (proj / "pwned.txt").exists() and not (proj / "pwned2.txt").exists()
    assert asked == []                                      # 子 agent 不会弹确认，也没有卡住

    streamed = [c for c in fake.calls if not c.get("invoke")]
    child_calls = streamed[1:-1]
    assert "代码检视子代理" in child_calls[0]["system"]     # 用的是 code_reviewer 的提示词
    offered = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
               for t in child_calls[0]["tools"]}
    assert {"read_file", "grep", "bash", "git_changes", "skill_tool"} <= offered
    assert not {"write_file", "edit_file", "task_tool", "ask_user", "question"} & offered

    records = [json.loads(line) for line in settings.audit_log_path.read_text(encoding="utf-8").splitlines()]
    child = {(r["tool"], str((r.get("args") or {}).get("command", ""))): r for r in records if r["agent"] == "code_reviewer"}
    assert child[("git_changes", "")]["ok"] and "app.py" in child[("git_changes", "")]["result_preview"]
    assert child[("bash", "git status --short")]["decision"] == "executed"
    assert child[("bash", "echo hacked > pwned.txt")]["decision"] == "rejected_by_guard"
    assert child[("skill_tool", "")]["ok"]
    assert any(r["agent"] == "main" and r["tool"] == "task_tool" and r["ok"] for r in records)

    assert [e["tool_name"] for e in seen if e["type"] == "tool_call"][:3] == ["skill_tool", "git_changes", "bash"]
    assert all(e["agent"] == "code_reviewer" for e in seen)
    assert bundle.usage.model_calls >= len(streamed)        # 子 agent 的模型调用也计入 /usage


def test_subagent_follows_model_switch(tmp_path: Path):
    """没在 [subagents] 里配模型的子 agent，/model 切换后派出去的新任务用的是新模型。"""
    from mole_agent.agent import switch_model
    from mole_agent.models import ModelChoice

    settings = _settings(tmp_path)
    bundle = build_agent(settings)
    other = ProviderConfig(name="other", api_base="http://127.0.0.1:9/v2", api_key="k2", models=["other-model"])
    switch_model(bundle, settings, ModelChoice(other, "other-model"))

    sub = bundle.agent.create_subagent("code_reviewer", "sub-test")
    assert sub.deep_config.model is bundle.agent.deep_config.model
    assert sub.deep_config.model.model_config.model_name == "other-model"

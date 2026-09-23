"""技能相关测试：仓库自带技能的格式检查 + 技能加载 / 读取 / 沙箱。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Windows 没开「开发者模式」时普通用户不能建软链接，这类用例直接跳过。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不能创建软链接：{exc}")
REPO_SKILLS = sorted(
    p.parent for root in (REPO_ROOT / ".mole" / "skills", REPO_ROOT / "examples" / "skills")
    for p in root.rglob("SKILL.md")
)


# ---------------------------------------------------------------- 仓库自带技能的格式
@pytest.mark.parametrize("skill_dir", REPO_SKILLS, ids=lambda p: p.name)
def test_repo_skill_format(skill_dir: Path):
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md 必须以 YAML front matter 开头"
    meta = yaml.safe_load(match.group(1))
    assert meta["name"] == skill_dir.name, "技能名以目录名为准，front matter 的 name 要与目录名一致"
    assert 20 <= len(meta["description"]) <= 1024, "description 要写清触发场景，且不宜过长"
    # 正文里引用的 references/ 文件必须存在
    for ref in re.findall(r"`(references/[\w./-]+)`", text):
        assert (skill_dir / ref).is_file(), f"引用的文件不存在：{ref}"


def test_code_review_skill_present():
    assert (REPO_ROOT / ".mole" / "skills" / "code-review" / "SKILL.md").is_file()


# ---------------------------------------------------------------- 沙箱包含技能目录
def test_sandbox_roots_include_skill_dirs(tmp_path: Path):
    from mole_agent.agent import sandbox_roots
    from mole_agent.config import Settings

    proj, home, ext = tmp_path / "proj", tmp_path / "home", tmp_path / "elsewhere" / "linked"
    ext.mkdir(parents=True)
    (home / "skills").mkdir(parents=True)
    _symlink_or_skip(home / "skills" / "linked", ext)
    proj.mkdir()
    roots = sandbox_roots(Settings(project_dir=proj, home_dir=home))
    assert proj.resolve() in roots and (home / "workspace").resolve() in roots
    assert (proj / ".mole" / "skills").resolve() in roots and (home / "skills").resolve() in roots
    assert ext.resolve() in roots                        # 软链接进来的技能
    assert Path("/etc").resolve() not in roots


# ---------------------------------------------------------------- 端到端：加载与读取
async def test_skills_loaded_and_readable(tmp_path: Path):
    from openjiuwen.core.runner import Runner

    from mole_agent import cli
    from mole_agent.agent import build_agent
    from mole_agent.config import Settings
    from test_e2e_fake_llm import ScriptedModel

    proj, home = tmp_path / "proj", tmp_path / "home"
    shutil.copytree(REPO_ROOT / ".mole", proj / ".mole")                       # 项目级：code-review
    (home / "skills" / "global-demo").mkdir(parents=True)                       # 用户级
    (home / "skills" / "global-demo" / "SKILL.md").write_text(
        "---\nname: global-demo\ndescription: 全局示例技能，用于测试用户级技能目录能否被读取\n---\n全局技能正文\n",
        encoding="utf-8",
    )
    ext = tmp_path / "claude_skills" / "linked-demo"                            # 软链接进来的
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text(
        "---\nname: linked-demo\ndescription: 软链接进来的技能，用于测试沙箱是否包含链接目标\n---\n软链接技能正文\n",
        encoding="utf-8",
    )
    _symlink_or_skip(home / "skills" / "linked-demo", ext)

    settings = Settings(model="fake", api_key="k", api_base="http://127.0.0.1:9/v1",
                        project_dir=proj, home_dir=home)
    bundle = build_agent(settings)
    fake = ScriptedModel()
    model = bundle.agent.deep_config.model
    model.stream, model.invoke = fake.stream, fake.invoke
    repl = cli.Repl(settings, bundle)

    async def auto_yes(_msg: str) -> str:
        return "y"

    repl._ask = auto_yes
    fake.plan(
        ("", [("skill_tool", {"skill_name": "code-review"})]),
        ("", [("skill_tool", {"skill_name": "code-review",
                              "relative_file_path": "references/mole-conventions.md"})]),
        ("", [("skill_tool", {"skill_name": "global-demo"})]),
        ("", [("skill_tool", {"skill_name": "linked-demo"})]),
        ("", [("read_file", {"file_path": "/etc/hosts"})]),
        ("完成", []),
    )
    await Runner.start()
    try:
        await repl.run_turn("帮我检视一下改动")
    finally:
        await Runner.stop()

    assert "code-review" in fake.calls[0]["system"]          # 技能出现在系统提示词的技能列表里
    import json
    results = [json.loads(line) for line in settings.audit_log_path.read_text(encoding="utf-8").splitlines()]
    by_key = {(r["tool"], (r["args"] or {}).get("skill_name"), (r["args"] or {}).get("relative_file_path")): r
              for r in results}
    assert by_key[("skill_tool", "code-review", None)]["ok"]
    assert "代码检视" in by_key[("skill_tool", "code-review", None)]["result_preview"]
    assert by_key[("skill_tool", "code-review", "references/mole-conventions.md")]["ok"]
    assert by_key[("skill_tool", "global-demo", None)]["ok"]
    assert by_key[("skill_tool", "linked-demo", None)]["ok"]
    assert not by_key[("read_file", None, None)]["ok"]      # 技能目录之外仍然受沙箱限制

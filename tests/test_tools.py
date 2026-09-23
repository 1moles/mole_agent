"""tools/ 目录的约定：自动发现、文件名即工具名、模板可用、写错时报清楚的错。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from mole_agent.config import Settings
from mole_agent.tools import ToolLoadError, build_custom_tools, discover_tool_modules
from mole_agent.tools._common import resolve_inside, truncate


def _settings(tmp_path: Path) -> Settings:
    return Settings(project_dir=tmp_path, home_dir=tmp_path / ".home")


def test_discovers_every_tool_file(tmp_path: Path):
    files = sorted(p.stem for p in Path(importlib.import_module("mole_agent.tools").__path__[0]).glob("*.py")
                   if not p.stem.startswith("_"))
    assert [m.__name__.rsplit(".", 1)[-1] for m in discover_tool_modules()] == files
    assert {"project_overview", "git_changes"} <= set(files)


def test_file_name_equals_tool_name(tmp_path: Path):
    names = [t.card.name for t in build_custom_tools(_settings(tmp_path))]
    files = [m.__name__.rsplit(".", 1)[-1] for m in discover_tool_modules()]
    assert names == files, "约定：tools/<工具名>.py，文件名与工具名一致，方便查找"


def test_every_tool_has_description_and_schema(tmp_path: Path):
    for t in build_custom_tools(_settings(tmp_path)):
        info = t.card.tool_info()
        assert len(info.description) >= 10, f"{info.name} 的 description 太短，模型无法判断何时使用"
        assert t.card.input_params and t.card.input_params.get("type") == "object"


async def test_template_is_a_working_tool(tmp_path: Path):
    from mole_agent.tools import _template

    (tmp_path / "a.txt").write_text("1\n2\n3\n", encoding="utf-8")
    t = _template.create(_settings(tmp_path))
    assert t.card.name == "count_lines"
    assert await t.invoke({"path": "a.txt"}) == "a.txt: 3 行"
    assert "count_lines" not in [x.card.name for x in build_custom_tools(_settings(tmp_path))]  # 模板不会被加载


def test_resolve_inside_and_truncate(tmp_path: Path):
    assert resolve_inside(tmp_path, "sub/x.py") == (tmp_path / "sub" / "x.py").resolve()
    for bad in ("../x", "/etc/passwd"):
        with pytest.raises(ValueError, match="越界"):
            resolve_inside(tmp_path, bad)
    assert truncate("abc", 10) == "abc"
    cut = truncate("x" * 50, 10, "用 read_file 看全文")
    assert cut.startswith("x" * 10) and "已截断到 10 字符" in cut and "read_file" in cut


def test_broken_tool_module_reports_file(tmp_path: Path, monkeypatch):
    import mole_agent.tools as tools_pkg

    pkg_dir = tmp_path / "fake_tools"
    pkg_dir.mkdir()
    (pkg_dir / "no_create.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(tools_pkg, "__path__", [str(pkg_dir)])
    monkeypatch.setattr(tools_pkg, "__name__", "fake_tools")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ToolLoadError, match="no_create.py 缺少 create"):
        discover_tool_modules()


def test_duplicate_tool_names_rejected(tmp_path: Path, monkeypatch):
    import mole_agent.tools as tools_pkg
    from mole_agent.tools import project_overview

    monkeypatch.setattr(tools_pkg, "discover_tool_modules", lambda: [project_overview, project_overview])
    with pytest.raises(ToolLoadError, match="重复"):
        tools_pkg.build_custom_tools(_settings(tmp_path))


def test_enabled_hook_can_skip_tool(tmp_path: Path, monkeypatch):
    import types

    import mole_agent.tools as tools_pkg
    from mole_agent.tools import git_changes

    off = types.ModuleType("mole_agent.tools.off")
    off.create = git_changes.create
    off.enabled = lambda settings: False
    monkeypatch.setattr(tools_pkg, "discover_tool_modules", lambda: [off])
    assert tools_pkg.build_custom_tools(_settings(tmp_path)) == []

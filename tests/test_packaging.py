"""Windows 便携包的打包脚本（packaging/windows）。脚本只能在 Windows 上运行，这里检查不依赖 Windows 的部分。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIN = ROOT / "packaging" / "windows"


def test_powershell_and_text_files_have_utf8_bom():
    # Windows PowerShell 5.1 把没有 BOM 的脚本按系统代码页（中文系统是 GBK）读，里面的中文会乱码甚至语法出错；
    # 旧版记事本也要靠 BOM 才认得 UTF-8
    for name in ("build.ps1", "install.ps1", "README-Windows.txt"):
        assert (WIN / name).read_bytes().startswith(b"\xef\xbb\xbf"), name


def test_build_script_references_existing_files():
    script = (WIN / "build.ps1").read_text(encoding="utf-8-sig")
    for name in re.findall(r'Join-Path \$PSScriptRoot "([^"]+)"', script):
        if name != "..\\..":
            assert (WIN / name).is_file(), name
    for name in re.findall(r'Join-Path \$RepoRoot "([^"]+)"', script):
        if name in {"dist", "build\\windows-cache", "models.toml"}:
            continue   # 输出目录；models.toml 不存在时用 models.example.toml
        assert (ROOT / name.replace("\\", "/")).exists(), name


def test_launchers_run_mole_agent_isolated():
    # 启动器和 mole.cmd 都要用 -I（不让项目目录里的同名模块盖掉 Mole）和 -X utf8，并把参数原样转发
    launcher = (WIN / "launcher.cs").read_text(encoding="utf-8")
    assert '"-I -X utf8 -m mole_agent"' in launcher
    script = (WIN / "build.ps1").read_text(encoding="utf-8-sig")
    assert "-I -X utf8 -m mole_agent %*" in script
    assert '"..\\..\\..`r`n"' in script   # mole_root.pth：site-packages → Lib → python → 包根目录


def test_workflow_builds_with_the_script():
    path = ROOT / ".github" / "workflows" / "windows-package.yml"
    if not path.is_file():
        pytest.skip("还没有放入 GitHub Actions 的打包流程")
    workflow = path.read_text(encoding="utf-8")
    assert "packaging\\windows\\build.ps1" in workflow and "runs-on: windows-latest" in workflow

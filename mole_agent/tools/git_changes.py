"""git_changes：取出当前仓库的未提交改动（或暂存区改动），供代码检视、写提交信息使用。只读。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, tool

from mole_agent.config import Settings
from mole_agent.tools._common import truncate

GIT_TIMEOUT_SECONDS = 30


def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or proc.stderr)


def changes(root: Path, staged: bool | None = False, max_chars: int | None = 30_000) -> str:
    """同步实现，方便单独测试。"""
    code, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return "当前项目不是 git 仓库。"
    limit = max(2_000, min(int(max_chars or 30_000), 120_000))
    diff_args = ["diff", "--cached"] if staged else ["diff", "HEAD"]

    _, status = _git(root, "status", "--short")
    _, stat = _git(root, *diff_args, "--stat")
    _, diff = _git(root, *diff_args, "--no-color", "--unified=3")
    if not diff.strip() and not staged:
        # 仓库还没有任何提交时 HEAD 不存在，退回到工作区 diff
        _, diff = _git(root, "diff", "--no-color", "--unified=3")

    parts = [
        "## git status --short", status.strip() or "（干净）",
        "## diff --stat", stat.strip() or "（无）",
        "## diff", diff.strip() or "（无改动；注意未跟踪的新文件不会出现在 diff 中，见 status 中的 ?? 行）",
    ]
    return truncate("\n".join(parts), limit, "可用 read_file 查看具体文件")


def create(settings: Settings) -> Tool:
    root = settings.project_dir

    async def git_changes(staged: bool = False, max_chars: int = 30_000) -> str:
        return await asyncio.to_thread(changes, root, staged, max_chars)

    return tool(
        git_changes,
        name="git_changes",
        description=(
            "只读：返回当前仓库的 git status、diff --stat 和完整 diff（相对 HEAD，或 staged=true 时只看暂存区）。"
            "做代码检视、写提交信息、确认自己改了什么时使用。"
        ),
        input_params={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "true 只看已 git add 的改动", "default": False},
                "max_chars": {"type": "integer", "description": "输出上限字符数，默认 30000", "default": 30000},
            },
            "required": [],
        },
    )

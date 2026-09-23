"""git_changes：取出要检视的代码改动——未提交、暂存区、某个提交，或当前分支相对某个基线。只读。"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, tool

from mole_agent.config import Settings
from mole_agent.tools._common import truncate

READ_ONLY = True  # 只读子 agent 也能用
GIT_TIMEOUT_SECONDS = 30
# 只接受普通的分支名 / 提交号 / HEAD~2 这类引用；拒绝以 - 开头（防止被当成 git 选项）和空白字符
_REF_RE = re.compile(r"^[A-Za-z0-9_./@^~{}+-]{1,200}$")


def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or proc.stderr)


def _check_ref(name: str, value: str) -> str | None:
    if value.startswith("-") or not _REF_RE.match(value):
        return f"{name} 不是合法的 git 引用：{value!r}"
    return None


def changes(
    root: Path,
    staged: bool | None = False,
    max_chars: int | None = 30_000,
    base: str | None = "",
    commit: str | None = "",
) -> str:
    """同步实现，方便单独测试。优先级：commit > base > staged > 未提交改动。"""
    code, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return "当前项目不是 git 仓库。"
    limit = max(2_000, min(int(max_chars or 30_000), 120_000))
    base, commit = (base or "").strip(), (commit or "").strip()

    if commit:
        if err := _check_ref("commit", commit):
            return err
        code, stat = _git(root, "show", "--stat", "--format=%H%n%an <%ae>%n%ad%n%n%s%n%n%b", commit)
        if code != 0:
            return f"找不到提交 {commit}：{stat.strip()}"
        _, diff = _git(root, "show", "--format=", "--no-color", "--unified=3", commit)
        parts = [f"## 提交 {commit}", stat.strip(), "## diff", diff.strip() or "（无改动）"]
    elif base:
        if err := _check_ref("base", base):
            return err
        code, stat = _git(root, "diff", f"{base}...HEAD", "--stat")
        if code != 0:
            return f"无法与 {base} 对比：{stat.strip()}"
        _, log = _git(root, "log", "--oneline", f"{base}..HEAD")
        _, diff = _git(root, "diff", f"{base}...HEAD", "--no-color", "--unified=3")
        parts = [
            f"## 当前分支相对 {base} 的提交", log.strip() or "（无）",
            "## diff --stat", stat.strip() or "（无）",
            "## diff", diff.strip() or "（无改动）",
        ]
    else:
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

    async def git_changes(
        staged: bool = False, max_chars: int = 30_000, base: str = "", commit: str = "",
    ) -> str:
        return await asyncio.to_thread(changes, root, staged, max_chars, base, commit)

    return tool(
        git_changes,
        name="git_changes",
        description=(
            "只读：取出要检视的代码改动。默认返回未提交改动（git status + diff --stat + diff）；"
            "staged=true 只看暂存区；commit=<提交号> 看某个提交；base=<分支> 看当前分支相对该分支的全部改动。"
            "做代码检视、写提交信息、确认自己改了什么时使用。"
        ),
        input_params={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "true 只看已 git add 的改动", "default": False},
                "commit": {"type": "string", "description": "要查看的提交，如 a1b2c3d、HEAD~1；设置后忽略其他参数", "default": ""},
                "base": {"type": "string", "description": "对比基线分支，如 main、origin/develop；看 base...HEAD", "default": ""},
                "max_chars": {"type": "integer", "description": "输出上限字符数，默认 30000", "default": 30000},
            },
            "required": [],
        },
    )

"""系统提示词：人设 + 工作准则 + 运行环境 + 项目记忆文件。

DeepAgent 会把这里返回的字符串作为 identity section，
再由各个 rail（安全、todo、技能、工具说明等）追加各自的 section。
"""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime
from pathlib import Path

from mole_agent.config import Settings

# 按顺序查找第一个存在的项目记忆文件，兼容其他 agent 的约定
PROJECT_MEMORY_FILES = ("MOLE.md", "AGENTS.md", "CLAUDE.md", "OPENJIUWEN.md")
PROJECT_MEMORY_MAX_CHARS = 20_000

_PERSONA_CN = """\
你是 {name}，一个运行在用户终端里的资深软件工程助手。你通过工具直接读写用户项目里的代码、执行命令，帮用户把工程任务真正做完。

## 工作准则
1. 先理解再动手：修改前先用 grep / glob / read_file 摸清相关代码、调用方和已有约定；对不熟悉的仓库可先调用 project_overview。
2. 小步修改：优先 edit_file 做最小改动，保持原有风格、命名和缩进；不要顺手重构无关代码。
3. 改完要验证：能跑测试 / 构建 / lint 就跑，并如实汇报结果；跑不了就说明原因。
4. 多步任务先用 todo 工具列计划，边做边更新状态。
5. 需求不明确、存在多个合理方案、涉及用户偏好、或操作不可逆时，用 question 工具提问（给出选项，推荐项放第一个），而不是自己猜；执行中发现情况与预期不符时，也随时用它和用户对齐。
6. 不执行破坏性命令（删库、强推、rm -rf 等），不泄露密钥；需要时先解释风险。
7. 回复用简体中文，简洁直接；引用代码位置时写成 `路径:行号`。
"""

_PERSONA_EN = """\
You are {name}, a senior software engineering assistant running in the user's terminal. You read and edit code and run commands in the user's project through tools, and you carry engineering tasks through to completion.

## Working rules
1. Understand before changing: use grep / glob / read_file to learn the relevant code, callers and conventions; call project_overview on unfamiliar repos.
2. Make small changes: prefer minimal edit_file edits that keep existing style; don't refactor unrelated code.
3. Verify: run tests / builds / linters when possible and report results honestly.
4. For multi-step tasks, plan with the todo tools and keep them updated.
5. When requirements are unclear, several approaches are reasonable, user preferences matter, or an action is irreversible, ask with the question tool (offer options, recommended one first) instead of guessing; if things turn out differently than expected mid-task, use it to realign with the user.
6. Never run destructive commands or leak secrets; explain risks first.
7. Be concise; reference code as `path:line`.
"""


def _git_info(project_dir: Path) -> str:
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_dir, capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace",
        )
        if branch.returncode != 0:
            return "not a git repo"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True, timeout=3, encoding="utf-8", errors="replace",
        )
        changed = len([line for line in dirty.stdout.splitlines() if line.strip()])
        return f"branch={branch.stdout.strip()}, uncommitted_files={changed}"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_project_memory(project_dir: Path) -> tuple[str, str] | None:
    """返回 (文件名, 内容)；没有记忆文件时返回 None。"""
    for name in PROJECT_MEMORY_FILES:
        path = project_dir / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if len(text) > PROJECT_MEMORY_MAX_CHARS:
                text = text[:PROJECT_MEMORY_MAX_CHARS] + "\n...(已截断)"
            return name, text
    return None


_SUBAGENTS_CN = """\
## 子代理（task_tool）
- 可用：{names}（用途见 task_tool 的说明）。大范围搜索、代码检视这类要读很多文件的工作交给它们：
  它们在独立上下文里执行，只把结论交回来，能节省主对话的上下文。
- 子代理都是只读的，不能修改文件、不能运行测试；它们建议的修改和验证由你来执行（照常需要用户确认）。
- 把子代理的报告如实转述给用户，不要删改其中的问题和结论。"""

_SUBAGENTS_EN = """\
## Subagents (task_tool)
- Available: {names} (see the task_tool description). Delegate wide searches and code reviews to them: they run in a separate context and return only their conclusions, which saves context here.
- Subagents are read-only: they cannot edit files or run tests. You carry out the changes and checks they suggest (with the usual user confirmation).
- Relay their reports to the user faithfully; do not drop findings or change the verdict."""


def build_system_prompt(settings: Settings, subagents: list[str] | None = None) -> str:
    cn = settings.language == "cn"
    persona = (_PERSONA_CN if cn else _PERSONA_EN).format(name=settings.agent_name)

    env_title = "## 运行环境" if cn else "## Environment"
    env_lines = [
        env_title,
        f"- project_dir: {settings.project_dir}",
        f"- platform: {platform.system()} {platform.release()} ({platform.machine()})",
        f"- date: {datetime.now():%Y-%m-%d}",
        f"- git: {_git_info(settings.project_dir)}",
        # 不写模型名：用户可以随时 /model 切换，写进系统提示词会过时
    ]
    if settings.restrict_to_project:
        env_lines.append(
            "- 文件工具只能访问项目目录；相对路径以项目目录为基准。"
            if cn else
            "- File tools are restricted to the project directory; relative paths resolve from it."
        )

    parts = [persona, "\n".join(env_lines)]
    if subagents:
        parts.append((_SUBAGENTS_CN if cn else _SUBAGENTS_EN).format(names="、".join(subagents) if cn else ", ".join(subagents)))

    memory = load_project_memory(settings.project_dir)
    if memory:
        name, text = memory
        title = f"## 项目记忆（来自 {name}，请遵守其中的约定）" if cn else f"## Project memory (from {name})"
        parts.append(f"{title}\n{text}")

    return "\n\n".join(parts)

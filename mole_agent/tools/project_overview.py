"""project_overview：快速摸清陌生仓库——语言分布、构建 / 依赖文件、顶层目录树。只读。"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, tool

from mole_agent.config import Settings
from mole_agent.tools._common import resolve_inside

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "target",
    "out", ".idea", ".vscode", ".gradle", ".next", ".nuxt", "vendor", "third_party",
}
BUILD_FILES = {
    "pyproject.toml", "setup.py", "requirements.txt", "package.json", "go.mod",
    "pom.xml", "build.gradle", "build.gradle.kts", "CMakeLists.txt", "Makefile",
    "Cargo.toml", "BUILD", "WORKSPACE", "meson.build", "configure.ac",
}
LANG_BY_EXT = {
    ".py": "Python", ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".c": "C", ".h": "C/C++ header",
    ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C/C++ header", ".rs": "Rust",
    ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".sh": "Shell", ".sql": "SQL", ".proto": "Protobuf", ".md": "Markdown",
    ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".toml": "TOML", ".cs": "C#",
    ".rb": "Ruby", ".php": "PHP", ".scala": "Scala", ".lua": "Lua", ".swift": "Swift",
}
MAX_FILES_SCANNED = 20_000
MAX_BYTES_FOR_LINE_COUNT = 2 * 1024 * 1024
MAX_ENTRIES_PER_DIR = 30


def overview(root: Path, path: str | None = ".", max_depth: int | None = 2) -> str:
    """同步实现，方便单独测试。"""
    base = resolve_inside(root, path)
    if not base.is_dir():
        return f"不是目录：{base}"
    depth_limit = max(1, min(int(max_depth or 2), 4))

    files_by_lang: Counter[str] = Counter()
    lines_by_lang: Counter[str] = Counter()
    build_files: list[str] = []
    scanned = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(base):
        # 原地裁剪，避免钻进 node_modules / .git 之类的大目录
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        current = Path(dirpath)
        for fname in filenames:
            scanned += 1
            if scanned > MAX_FILES_SCANNED:
                truncated = True
                break
            p = current / fname
            rel = p.relative_to(base)
            if fname in BUILD_FILES and len(rel.parts) <= 3:
                build_files.append(str(rel))
            lang = LANG_BY_EXT.get(p.suffix.lower())
            if not lang:
                continue
            files_by_lang[lang] += 1
            try:
                if p.stat().st_size <= MAX_BYTES_FOR_LINE_COUNT:
                    with p.open("rb") as fh:
                        lines_by_lang[lang] += sum(1 for _ in fh)
            except OSError:
                pass
        if truncated:
            break

    tree: list[str] = []

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        shown = 0
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith("."):
                continue
            shown += 1
            if shown > MAX_ENTRIES_PER_DIR:
                tree.append("  " * depth + "…")
                break
            tree.append("  " * depth + (e.name + "/" if e.is_dir() else e.name))
            if e.is_dir() and depth + 1 < depth_limit:
                walk(e, depth + 1)

    walk(base, 0)

    out = [f"# 项目概览：{base}"]
    if files_by_lang:
        out.append("\n## 语言分布（文件数 / 行数）")
        for lang, n in files_by_lang.most_common(12):
            out.append(f"- {lang}: {n} files / {lines_by_lang[lang]} lines")
    out.append("\n## 构建 / 依赖文件")
    if build_files:
        out.extend(f"- {f}" for f in sorted(build_files)[:30])
    else:
        out.append("- （未发现）")
    out.append(f"\n## 目录结构（深度 {depth_limit}）")
    out.extend(tree or ["（空）"])
    if truncated:
        out.append(f"\n（文件数超过 {MAX_FILES_SCANNED}，统计已截断）")
    return "\n".join(out)


def create(settings: Settings) -> Tool:
    root = settings.project_dir

    async def project_overview(path: str = ".", max_depth: int = 2) -> str:
        return await asyncio.to_thread(overview, root, path, max_depth)

    return tool(
        project_overview,
        name="project_overview",
        description=(
            "只读：扫描项目目录，返回语言分布（文件数/行数）、构建与依赖文件、顶层目录树。"
            "接手陌生仓库或需要整体理解工程结构时先调用它。"
        ),
        input_params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的子目录，默认 '.'", "default": "."},
                "max_depth": {"type": "integer", "description": "目录树展开深度 1-4，默认 2", "default": 2},
            },
            "required": [],
        },
    )

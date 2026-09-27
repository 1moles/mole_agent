"""项目规则文件的发现和读取；不依赖 Agent 或长期记忆 SDK。"""

from pathlib import Path

# 按顺序查找第一个存在的项目记忆文件，兼容其他 agent 的约定
PROJECT_MEMORY_FILES = ("MOLE.md", "AGENTS.md", "CLAUDE.md", "OPENJIUWEN.md")
PROJECT_MEMORY_MAX_CHARS = 20_000


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


def init_target(project_dir: Path) -> Path:
    """更新当前生效文件；无规则时创建 AGENTS.md。"""
    loaded = load_project_memory(project_dir)
    return project_dir / (loaded[0] if loaded else "AGENTS.md")

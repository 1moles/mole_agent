"""/init 的草稿生命周期；项目分析由正常 Agent 执行。"""
from __future__ import annotations

import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path

from mole_agent.instructions import PROJECT_MEMORY_MAX_CHARS, init_target


class InitDraft:
    def __init__(self, project_dir: Path, workspace_dir: Path) -> None:
        self.project_dir = project_dir.resolve()
        self.target = init_target(self.project_dir)
        if self.target.is_symlink():
            raise ValueError("项目规则文件是符号链接，请直接编辑其目标文件")
        self.before = self.target.read_bytes() if self.target.exists() else None
        self.mode = stat.S_IMODE(self.target.stat().st_mode) if self.before is not None else 0o644
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self._directory = tempfile.TemporaryDirectory(prefix="init-", dir=workspace_dir)
        self.path = Path(self._directory.name) / "draft.md"

    def prompt(self, focus: str) -> str:
        template = files("mole_agent.commands").joinpath("templates/init.md").read_text(encoding="utf-8")
        return template + (
            f"\n项目目录：{self.project_dir}\n目标规则文件：{self.target}\n"
            f"草稿输出文件：{self.path}\n用户补充要求：{focus or '无'}\n"
        )

    def _check_unchanged(self) -> None:
        current = self.target.read_bytes() if self.target.exists() else None
        if self.target.is_symlink() or current != self.before or init_target(self.project_dir) != self.target:
            raise ValueError("项目规则已被其他操作修改，目标文件未写回，请重新执行 /init")

    def commit(self) -> str:
        if not self.path.is_file() or self.path.is_symlink():
            raise ValueError("未生成有效草稿，目标文件未写回")
        # 解码只用于校验；比较和写回保留原始字节，避免转换 CRLF 换行。
        data = self.path.read_bytes()
        content = data.decode("utf-8")
        if not content.strip():
            raise ValueError("草稿为空，目标文件未写回")
        if len(content.strip()) > PROJECT_MEMORY_MAX_CHARS:
            raise ValueError(f"草稿超过 {PROJECT_MEMORY_MAX_CHARS} 字符，请精简后重试；目标文件未写回")
        self._check_unchanged()
        if data == self.before:
            return "unchanged"
        # Stage on the target filesystem so replacement works across workspace mounts.
        fd, name = tempfile.mkstemp(prefix=".mole-init-", dir=self.project_dir)
        staged = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            staged.chmod(self.mode)
            self._check_unchanged()
            os.replace(staged, self.target)
        finally:
            staged.unlink(missing_ok=True)
        return "created" if self.before is None else "updated"

    def close(self) -> None:
        self._directory.cleanup()

    def __enter__(self) -> InitDraft:
        return self

    def __exit__(self, *args) -> None:
        self.close()

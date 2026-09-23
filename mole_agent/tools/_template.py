"""新工具模板（以下划线开头，不会被加载）。

用法：复制本文件为 `<工具名>.py`（全小写下划线，和工具名一致），改好后重启 mole 即可，
不需要改其他文件。下面是一个可以直接运行的示例：统计某个文件的行数。

检查清单：
- create(settings) 返回 Tool；需要按配置开关时再加 enabled(settings) -> bool
- description 写清「做什么 + 什么时候该用」，只读工具在开头注明「只读」
- 参数写进 input_params 的 JSON Schema，并给默认值和说明
- 阻塞 IO（读文件、子进程、网络）放进 asyncio.to_thread；子进程必须带 timeout
- 路径参数一律经过 resolve_inside，限制在项目目录内
- 输出要有上限，用 truncate 截断并提示模型怎么看完整内容
- 在 tests/ 里给同步实现函数写测试
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, tool

from mole_agent.config import Settings
from mole_agent.tools._common import resolve_inside, truncate

MAX_OUTPUT_CHARS = 4_000


def run(root: Path, path: str) -> str:
    """同步实现：真正干活的逻辑放这里，方便单独测试。"""
    target = resolve_inside(root, path)
    if not target.is_file():
        return f"不是文件：{path}"
    with target.open("rb") as fh:
        lines = sum(1 for _ in fh)
    return truncate(f"{path}: {lines} 行", MAX_OUTPUT_CHARS)


def create(settings: Settings) -> Tool:
    root = settings.project_dir

    async def count_lines(path: str) -> str:
        return await asyncio.to_thread(run, root, path)

    return tool(
        count_lines,
        name="count_lines",
        description="只读：统计项目内某个文件的行数。需要快速了解文件规模时使用。",
        input_params={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径"},
            },
            "required": ["path"],
        },
    )

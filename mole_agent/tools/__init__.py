"""自定义工具：每个工具一个文件，启动时自动发现并注册。

SysOperationRail 已经提供 read_file / write_file / edit_file / glob / grep / list_files / bash
等通用工具，这里放 Mole 自己补充的工具。

新增工具：复制 `_template.py` 为 `<工具名>.py`，改好后重启 mole 即可，不需要改其他文件。
每个工具模块的约定：
    create(settings) -> Tool          必须：创建工具实例
    enabled(settings) -> bool         可选：返回 False 时本次不注册（例如依赖某项配置）
    READ_ONLY = True                  可选：声明工具不改任何东西，只读子 agent（subagents/）也会拿到它；
                                      不写就只给主 agent（主 agent 的写操作有人工确认兜底，子 agent 没有）
以下划线开头的模块（`_common.py`、`_template.py`）不会被当成工具加载。
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from openjiuwen.core.foundation.tool import Tool

from mole_agent.config import Settings


class ToolLoadError(RuntimeError):
    """某个工具模块写法不对或创建失败。直接报错而不是跳过，免得工具悄悄消失。"""


def discover_tool_modules() -> list[ModuleType]:
    """按文件名排序返回所有工具模块，保证每次注册顺序一致。"""
    modules: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.ispkg:
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001 —— 统一包装成带文件名的错误
            raise ToolLoadError(f"加载工具模块 tools/{info.name}.py 失败：{exc}") from exc
        if not callable(getattr(module, "create", None)):
            raise ToolLoadError(f"tools/{info.name}.py 缺少 create(settings) -> Tool")
        modules.append(module)
    return modules


def build_custom_tools(settings: Settings, *, read_only: bool = False) -> list[Tool]:
    """read_only=True 时只返回声明了 READ_ONLY = True 的工具（给只读子 agent 用）。"""
    tools: list[Tool] = []
    names: dict[str, str] = {}
    for module in discover_tool_modules():
        file = module.__name__.rsplit(".", 1)[-1]
        if read_only and getattr(module, "READ_ONLY", False) is not True:
            continue
        enabled = getattr(module, "enabled", None)
        if callable(enabled) and not enabled(settings):
            continue
        try:
            instance = module.create(settings)
        except Exception as exc:  # noqa: BLE001
            raise ToolLoadError(f"tools/{file}.py 的 create() 出错：{exc}") from exc
        if not isinstance(instance, Tool):
            raise ToolLoadError(f"tools/{file}.py 的 create() 应返回 Tool，实际是 {type(instance).__name__}")
        name = instance.card.name
        if name in names:
            raise ToolLoadError(f"工具名 {name!r} 重复：tools/{names[name]}.py 与 tools/{file}.py")
        names[name] = file
        tools.append(instance)
    return tools


__all__ = ["ToolLoadError", "build_custom_tools", "discover_tool_modules"]

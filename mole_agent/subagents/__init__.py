"""子 agent：每个子 agent 一个文件，启动时自动发现，主 agent 通过 task_tool 把任务派给它们。

子 agent 由 openjiuwen 在每次派任务时现场创建：上下文独立（只看到任务描述），跑完把最终答复
交回主 agent。Mole 给所有子 agent 统一套上：只读文件工具、只读 shell 守卫、审计、共享的 token
统计，以及和主 agent 相同的文件沙箱（见 `_common.SubagentEnv`）。

新增子 agent：在本目录新建 `<子agent名>.py`（文件名与子 agent 名一致），提供
    create(env: SubagentEnv) -> SubAgentConfig      必须
    enabled(settings) -> bool                        可选
重启 mole 即可。用哪个模型在 models.toml 的 [subagents] 表里配，没配就跟随主 agent 当前模型。
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

from openjiuwen.harness.schema.config import SubAgentConfig

from mole_agent.subagents._common import SubagentEnv


class SubagentLoadError(RuntimeError):
    """某个子 agent 模块写法不对或创建失败。"""


def discover_subagent_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name.startswith("_") or info.ispkg:
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001
            raise SubagentLoadError(f"加载子 agent 模块 subagents/{info.name}.py 失败：{exc}") from exc
        if not callable(getattr(module, "create", None)):
            raise SubagentLoadError(f"subagents/{info.name}.py 缺少 create(env) -> SubAgentConfig")
        modules.append(module)
    return modules


def build_subagents(env: SubagentEnv) -> list[SubAgentConfig]:
    configs: list[SubAgentConfig] = []
    names: dict[str, str] = {}
    for module in discover_subagent_modules():
        file = module.__name__.rsplit(".", 1)[-1]
        enabled = getattr(module, "enabled", None)
        if callable(enabled) and not enabled(env.settings):
            continue
        try:
            config = module.create(env)
        except Exception as exc:  # noqa: BLE001
            raise SubagentLoadError(f"subagents/{file}.py 的 create() 出错：{exc}") from exc
        if not isinstance(config, SubAgentConfig):
            raise SubagentLoadError(f"subagents/{file}.py 的 create() 应返回 SubAgentConfig")
        name = config.agent_card.name
        if name in names:
            raise SubagentLoadError(f"子 agent 名 {name!r} 重复：subagents/{names[name]}.py 与 subagents/{file}.py")
        names[name] = file
        configs.append(config)
    return configs


__all__ = ["SubagentEnv", "SubagentLoadError", "build_subagents", "discover_subagent_modules"]

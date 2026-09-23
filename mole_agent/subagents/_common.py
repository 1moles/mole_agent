"""子 agent 共用的运行环境：模型、只读 rails、工具、技能、沙箱。

为什么子 agent 都是只读的：task_tool 在内部跑子 agent，子 agent 触发的人工确认（中断）
不会传到用户终端（openjiuwen 0.1.18），挂 ApprovalRail 只会让它卡住。所以子 agent 不给写工具，
bash 由 ReadOnlyShellRail 限制为只读命令；需要改文件、跑测试时由子 agent 在报告里建议，主 agent 去做。
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.rails import SkillUseRail, SysOperationRail
from openjiuwen.harness.workspace.workspace import Workspace

from mole_agent.config import Settings
from mole_agent.models import ModelSelectionError
from mole_agent.rails import ActivityFeed, ReadOnlyShellRail, TokenUsageRail, ToolTraceRail
from mole_agent.tools import build_custom_tools

log = logging.getLogger("mole_agent")


def per_instance(factory: Callable[[], AgentRail]) -> AgentRail:
    """让 rail 每个子 agent 实例各用一份。

    SubAgentConfig 里放的是 rail 实例，SDK 每次派任务都会新建子 agent；rail 带有
    fork_for_agent() 时 create_subagent 会调用它换一个新实例，否则所有子 agent 实例共用同一个
    rail 对象（并行派两个任务时会互相覆盖工具列表等状态）。这里给实例挂上 fork_for_agent。
    """
    rail = factory()
    rail.fork_for_agent = lambda: per_instance(factory)  # type: ignore[attr-defined]
    return rail


@dataclass
class SubagentEnv:
    settings: Settings
    build_model: Callable[[Settings], Any]          # agent.build_model，注入进来避免循环 import
    sys_operation: Optional[SysOperation] = None    # 主 agent 的沙箱，子 agent 共用
    usage_rail: Optional[TokenUsageRail] = None     # 与主 agent 共用，/usage 显示合计
    feed: Optional[ActivityFeed] = None             # 子 agent 的工具调用转给终端显示
    warnings: list[str] = field(default_factory=list)

    def workspace(self, name: str) -> Workspace:
        """子 agent 的私有工作区。

        必须显式给：SDK 只有在 SubAgentConfig.workspace 也设置时才采用 sys_operation，
        否则子 agent 会另建一个默认沙箱，读不到技能目录。
        """
        path = self.settings.workspace_dir / "sub_agents" / name
        path.mkdir(parents=True, exist_ok=True)
        return Workspace(root_path=str(path), language=self.settings.language)

    def model_for(self, name: str) -> Optional[Any]:
        """models.toml [subagents] 里给这个子 agent 配的模型；返回 None 表示跟随主 agent 当前模型。"""
        spec = self.settings.catalog.subagent_models.get(name)
        if not spec:
            return None
        try:
            choice = self.settings.catalog.resolve(spec)
            problems = choice.provider.problems()
            if problems:
                raise ModelSelectionError("；".join(problems))
            trial = copy.copy(self.settings)
            trial.apply_choice(choice)
            return self.build_model(trial)
        except Exception as exc:  # noqa: BLE001 —— 子 agent 模型配错不影响启动，回落到主模型
            self.warnings.append(f"子 agent {name} 的模型 {spec} 不可用，改用主 agent 的模型：{exc}")
            log.warning(self.warnings[-1])
            return None

    def model_label(self, name: str) -> str:
        """终端显示用：这个子 agent 实际用的模型。"""
        spec = self.settings.catalog.subagent_models.get(name)
        failed = any(w.startswith(f"子 agent {name} ") for w in self.warnings)
        return spec if spec and not failed else "跟随主模型"

    def read_only_rails(self, name: str) -> list[AgentRail]:
        """只读文件工具 + 只读 shell + 审计/终端进度 + 共用 token 统计。"""
        settings = self.settings
        rails: list[AgentRail] = [
            per_instance(lambda: SysOperationRail(read_only=True)),   # read_file / glob / grep / list_files / bash
            per_instance(ReadOnlyShellRail),
            per_instance(lambda: ToolTraceRail(
                audit_path=settings.audit_log_path if settings.audit_log else None,
                project_dir=settings.project_dir,
                agent_name=name,
                feed=self.feed,
            )),
        ]
        if self.usage_rail is not None:
            rails.append(self.usage_rail)  # 故意共用同一个实例
        return rails

    def skill_rail(self) -> AgentRail:
        dirs = [str(p) for p in self.settings.skills_dirs]
        return per_instance(lambda: SkillUseRail(skills_dir=dirs, skill_mode="all", include_tools=False))

    def tools(self) -> list[Tool]:
        """声明了 READ_ONLY = True 的自定义工具。每种子 agent 各建一份，不和主 agent 共用 Tool 对象。"""
        return build_custom_tools(self.settings, read_only=True)

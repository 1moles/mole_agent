"""explore_agent：只读探索代码库。直接用 openjiuwen 自带的 Explore 子 agent。

提示词、描述（主 agent 靠它决定何时派活）、迭代上限都用 SDK 默认值；Mole 只替换 rails，
把它纳入自己的安全边界：只读文件工具 + 只读 shell 守卫 + 审计，以及与主 agent 相同的沙箱。
"""

from __future__ import annotations

from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.subagents.explore_agent import build_explore_agent_config

from mole_agent.subagents._common import SubagentEnv

NAME = "explore_agent"  # SDK 默认的子 agent 名，主 agent 用 task_tool(subagent_type="explore_agent") 调用


def create(env: SubagentEnv) -> SubAgentConfig:
    # restrict_to_work_dir 不用设：SDK 取「子 agent 配置 or 主 agent 配置」，主 agent 限制了子 agent 就限制
    return build_explore_agent_config(
        model=env.model_for(NAME),
        # 传了 rails 就会替换 SDK 默认的 [SysOperationRail(read_only=True)]，所以只读文件工具要自己带上
        rails=env.read_only_rails(NAME),
        tools=env.tools(),
        workspace=env.workspace(NAME),
        sys_operation=env.sys_operation,
        language=env.settings.language,
    )

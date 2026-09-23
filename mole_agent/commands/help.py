"""Compose help from registered commands; apply terminal styling separately."""
from rich.text import Text

from .registry import CommandRegistry
from .types import Help


INPUT_GUIDANCE = (
    "  ↑↓ 选择 · Tab 补全 · Esc 关闭菜单并保留输入\n"
    "  Enter 确认选中候选，再次 Enter 提交；没有选中候选时直接提交\n"
    "  Ctrl+C 打断正在执行的任务 · Ctrl+D 退出"
)
PROJECT_GUIDANCE = (
    "  项目根目录可放置 MOLE.md（也兼容 AGENTS.md / CLAUDE.md），"
    "写入项目约定，启动时自动加载。\n"
    "  子 Agent（explore_agent / code_reviewer）由主 Agent 按需调用，"
    "终端中以 │ 开头的行是它们的动作。"
)


def build_help(registry: CommandRegistry) -> Help:
    commands = registry.list_commands()
    rows = []
    examples = []
    for command in commands:
        aliases = ("（别名：" + "、".join("/" + a for a in command.aliases) + "）"
                   if command.aliases else "")
        rows.append(f"  {command.usage or '/' + command.name}  {command.description}{aliases}")
        examples.extend(f"  {invocation}  {description}"
                        for invocation, description in command.examples)
    sections = [("斜杠命令", "\n".join(rows))]
    if examples:
        sections.append(("命令示例", "\n".join(examples)))
    sections.extend([
        ("快捷键与输入", INPUT_GUIDANCE),
        ("项目与子 Agent", PROJECT_GUIDANCE),
    ])
    return Help(tuple(sections))


def render_help(help_result: Help) -> Text:
    """Keep descriptions literal, including brackets that resemble Rich markup."""
    output = Text()
    for index, (heading, body) in enumerate(help_result.sections):
        if index:
            output.append("\n\n")
        output.append(heading, style="bold")
        output.append("\n" + body)
    return output

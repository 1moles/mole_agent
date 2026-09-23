from .types import CommandSpec, CommandContext, CompletionItem, Message, AgentPrompt, Exit, Help
from .registry import CommandRegistry
from .dispatcher import dispatch
from .help import build_help
from .completion import SlashCompleter, command_key_bindings


def default_registry():
    registry = CommandRegistry()

    async def help_command(ctx, args):
        if args:
            return Message("用法：/help")
        return build_help(registry)

    registry.register(CommandSpec("help", "显示帮助", help_command))
    definitions = [
        ("model", "切换模型，保留对话上下文", "[序号|供应商/模型]", ()),
        ("models", "在线查询供应商模型", "[供应商]", ()),
        ("review", "只读代码检视", "[范围或要求]", ()),
        ("new", "新建会话，重置用量和临时授权", "", ()),
        ("usage", "查看本会话 Token 用量", "", ()),
        ("exit", "退出", "", ("quit", "q")),
    ]
    for name, description, usage, aliases in definitions:
        async def handler(ctx, args, name=name, usage=usage):
            if args and not usage:
                return Message(f"用法：/{name}")
            if name == "exit":
                return Exit()
            return await ctx.actions[name](args)

        def complete(ctx, prefix, name=name):
            return [item for item in ctx.candidates(name) if item.value.startswith(prefix)]

        registry.register(CommandSpec(
            name, description, handler, f"/{name}" + (f" {usage}" if usage else ""),
            aliases, complete if name in {"model", "models"} else None,
            examples=(
                ("/review", "检视当前未提交的改动"),
                ("/review 提交 a1b2c3d", "检视指定提交"),
                ("/review 和 main 比", "与指定分支比较"),
            ) if name == "review" else (),
        ))
    return registry

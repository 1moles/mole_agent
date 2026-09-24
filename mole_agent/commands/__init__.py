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
        ("history", "查看当前项目历史会话；带关键词时在标题和内容里模糊搜索", "[序号|关键词]", ()),
        ("resume", "恢复历史会话继续聊（可按关键词搜）；mole -c 继续最近会话", "[序号|关键词|会话id]", ()),
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
            examples={
                "review": (
                    ("/review", "检视当前未提交的改动"),
                    ("/review 提交 a1b2c3d", "检视指定提交"),
                    ("/review 和 main 比", "与指定分支比较"),
                ),
                "history": (
                    ("/history 你好", "搜索标题或内容里带「你好」的会话（不区分大小写，不要求开头匹配）"),
                    ('/history "8080"', "纯数字会被当成序号，加引号才按关键词搜"),
                ),
            }.get(name, ()),
        ))
    return registry

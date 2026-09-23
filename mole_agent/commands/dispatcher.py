from .types import Message


async def dispatch(registry, context, text):
    parts = text.split(maxsplit=1)
    name = parts[0][1:] if parts else ""
    args = parts[1] if len(parts) == 2 else ""
    spec = registry.resolve(name)
    if spec is None:
        suggestions = "、".join("/" + s.name for s in registry.match(name))
        return Message(f"未知命令 /{name}，" + (f"可用命令：{suggestions}" if suggestions else "输入 /help 查看"))
    try:
        reason = spec.availability(context) if spec.availability else None
        if reason:
            return Message(f"/{spec.name} 当前不可用：{reason}")
        return await spec.handler(context, args)
    except Exception as exc:
        return Message(f"/{spec.name} 执行失败：{exc}")

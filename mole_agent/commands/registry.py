import re
from .types import CommandSpec


class CommandRegistry:
    def __init__(self):
        self._commands = []
        self._names = {}

    def register(self, spec: CommandSpec):
        names = (spec.name, *spec.aliases)
        if (len(set(names)) != len(names)
                or any(not re.fullmatch(r"[a-z][a-z0-9_-]*", n) or n in self._names for n in names)):
            raise ValueError(f"无效或重复的命令名称：{names}")
        self._commands.append(spec)
        self._names.update((n, spec) for n in names)

    def resolve(self, name):
        return self._names.get(name.lower())

    def list_commands(self):
        return tuple(self._commands)

    def match(self, prefix):
        return [s for s in self._commands
                if any(n.startswith(prefix.lower()) for n in (s.name, *s.aliases))]

    def help_text(self):
        return "斜杠命令\n" + "\n".join(
            f"  {s.usage or '/' + s.name}  {s.description}" for s in self._commands
        ) + "\n\n↑↓ 选择 · Tab 补全 · Enter 确认选项后再次提交 · Esc 关闭菜单\nCtrl+C 中断 · Ctrl+D 退出"

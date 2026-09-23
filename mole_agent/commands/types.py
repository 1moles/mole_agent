"""Terminal-independent command contracts."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class Message:
    text: str


@dataclass(frozen=True)
class AgentPrompt:
    text: str


@dataclass(frozen=True)
class Exit:
    pass


@dataclass(frozen=True)
class CompletionItem:
    value: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class Help:
    sections: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CommandContext:
    """Capabilities bound afresh for each invocation; never retain an Agent."""
    actions: dict[str, Callable[[str], Awaitable[Any]]]
    candidates: Callable[[str], list[CompletionItem]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[CommandContext, str], Awaitable[Message | AgentPrompt | Exit | Help]]
    usage: str = ""
    aliases: tuple[str, ...] = ()
    complete_args: Callable | None = None
    availability: Callable | None = None
    examples: tuple[tuple[str, str], ...] = ()

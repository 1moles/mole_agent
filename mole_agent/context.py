"""上下文窗口：/context 查看占用、/compact 手动压缩、压缩提示、压缩器的模型适配。

压缩本身由 SDK 完成（agent.build_agent 挂的 ContextProcessorRail 预设处理器链）：
  - 每次调模型前按占用自动压缩（默认达到窗口的 80%）；
  - 模型报「上下文超长」时，ReActAgent 主动压缩一次再重试同一步（ContextEngine.recover_from_model_exception）；
  - 压缩过程以 context.compression_state 事件写进输出流，终端据此提示一行。

Mole 在这里补上几处：
  1. 压缩器按 ReActAgent 配置里的 model_client 自己建模型客户端、调非流式 invoke，只收流式请求的网关会拒绝；
     rails.CompressionRail 在 init 时把这份配置换成只走流式的客户端（agent.stream_only_client_config）。
  2. 压缩失败时 SDK 只写日志、事件里只有 noop；CompressionWatcher 订阅模型调用失败事件记下原因，终端能说清楚为什么没压。
  3. SDK 识别「上下文超长」只认英文报错，这里补上常见的中文写法（内部网关常见）。
  4. /context、/compact 用到的读取和压缩入口。

全部走 SDK 的公开接口。cli 在顶层导入本模块，所以这里的 openjiuwen 都在函数里按需导入。
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

log = logging.getLogger("mole_agent")

CONTEXT_ID = "default_context_id"   # DeepAgent 内层 ReActAgent 用的上下文 id（SDK 默认值）
OFFLOADERS = frozenset({"MessageSummaryOffloader"})   # 规则处理、不调模型：把大段工具输出转存，只留预览
_ERROR_HISTORY = 50


def fmt_tokens(n: Optional[int]) -> str:
    """12345 → 12.3k，1050000 → 1.05M。"""
    if n is None:
        return "?"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def failure_hint(reason: str, stream_only: bool) -> str:
    """压缩失败时给一句排查建议：最常见的是网关只收流式请求、而供应商没配 stream_only。"""
    if not stream_only and re.search(r"\b400\b|stream", reason, re.IGNORECASE):
        return "（网关只接受流式请求的话，在 models.toml 里给这个供应商加上 stream_only = true）"
    return ""


def describe_error(exc: BaseException) -> str:
    from mole_agent.netcheck import root_cause

    root = root_cause(exc)
    text = " ".join(str(root).split())
    if len(text) > 160:
        text = text[:159] + "…"
    return f"{type(root).__name__}: {text}" if text else type(root).__name__


# --------------------------------------------------------------------------- #
# 压缩失败的原因
# --------------------------------------------------------------------------- #
@dataclass
class CompressionWatcher:
    """订阅 SDK 的模型调用失败事件（LLMCallEvents.LLM_CALL_ERROR），只记压缩过程里的那些。

    压缩过程中的模型调用都在 SDK 的 context_compression_operation 作用域里，用
    current_context_compression_operation_id() 能拿到这次压缩的操作 id，与 context.compression_state 事件对得上。
    """

    stream_only: bool = False
    errors: list[tuple[int, str, str]] = field(default_factory=list)   # (序号, 压缩操作 id, 原因)
    error_seq: int = 0
    _framework: Any = None   # 已经订阅过的回调框架（Runner 重启后框架对象可能换了，要重新订阅）

    async def listen(self) -> None:
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.runner.callback.events import LLMCallEvents

        framework = Runner.callback_framework
        if framework is self._framework:
            return
        await framework.register(LLMCallEvents.LLM_CALL_ERROR, self.on_llm_error)
        self._framework = framework

    async def on_llm_error(self, *args: Any, error: Any = None, **kwargs: Any) -> None:
        operation = current_compression_operation()
        if operation and isinstance(error, BaseException):
            self.remember(error, operation)

    def remember(self, exc: BaseException, operation: str) -> None:
        self.error_seq += 1
        self.errors.append((self.error_seq, operation, describe_error(exc)))
        del self.errors[:-_ERROR_HISTORY]

    def error_for(self, operation_id: str) -> Optional[str]:
        """某次压缩操作里最后一次压缩模型调用失败的原因。"""
        for _, operation, reason in reversed(self.errors):
            if operation == operation_id:
                return reason
        return None

    def errors_since(self, seq: int) -> list[str]:
        return [reason for s, _, reason in self.errors if s > seq]


def current_compression_operation() -> str:
    try:
        from openjiuwen.core.context_engine.context.compression_scope import (
            current_context_compression_operation_id,
        )

        return current_context_compression_operation_id() or ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# 模型报「上下文超长」：让 SDK 的恢复也认中文报错
# --------------------------------------------------------------------------- #
# SDK 只认英文（context length / maximum context / prompt is too long ...），补上中文网关常见的写法
_ZH_OVERFLOW = re.compile(r"(上下文|输入|提示词|prompt|token)[^，。,.;；\n]{0,12}(超长|过长|超出|超过|超限)", re.IGNORECASE)


class ContextOverflowAlias(Exception):
    """中文的「上下文超长」报错换成 SDK 认得的英文说法，原异常挂在 __cause__ 上。"""


def _exception_texts(exc: BaseException) -> list[str]:
    texts, seen, current = [], set(), exc
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        texts.append(str(current))
        for attr in ("message", "body", "text"):
            value = getattr(current, attr, None)
            if isinstance(value, (str, dict)):
                texts.append(str(value))
        current = current.__cause__ or current.__context__
    return texts


def is_zh_context_overflow(exc: BaseException) -> bool:
    for text in _exception_texts(exc):
        for match in _ZH_OVERFLOW.finditer(text):
            around = text[max(0, match.start() - 6):match.end()].lower()
            if "输出" not in around and "max_" not in around:   # 输出长度、max_tokens 参数超限不是上下文超长
                return True
    return False


def extend_overflow_detection(engine: Any) -> None:
    """ReActAgent 在模型调用失败后调 context_engine.recover_from_model_exception：是超长报错就压缩一次再重试。
    SDK 明确支持在实例上替换这个方法（ReActAgent._recover_from_model_exception 的说明）；这里包一层，
    中文报错先换成它认得的英文说法再交给原方法。"""
    original = getattr(engine, "recover_from_model_exception", None)
    if not callable(original) or getattr(original, "_mole_extended", False):
        return

    async def recover_from_model_exception(*args: Any, exception: Any = None, **kwargs: Any) -> bool:
        if isinstance(exception, BaseException) and is_zh_context_overflow(exception):
            alias = ContextOverflowAlias(f"maximum context length exceeded: {exception}")
            alias.__cause__ = exception
            exception = alias
        return await original(*args, exception=exception, **kwargs)

    recover_from_model_exception._mole_extended = True  # type: ignore[attr-defined]
    engine.recover_from_model_exception = recover_from_model_exception


# --------------------------------------------------------------------------- #
# 读取上下文
# --------------------------------------------------------------------------- #
@dataclass
class OpenedContext:
    context: Any
    session: Any = None          # 为了从检查点恢复而新建的 Session
    loaded: bool = True          # False：agent 还没初始化（这次启动后还没发过消息），压缩器还没装上


@asynccontextmanager
async def opened_context(agent: Any, session_id: str, *, fresh_session: bool = False,
                         expect_compression: bool = False) -> AsyncIterator[OpenedContext]:
    """拿到会话的 ModelContext。

    这个进程里聊过的会话，上下文在 ContextEngine 的内存池里，直接用；刚续聊、还没发消息的会话从检查点恢复。
    fresh_session=True 时另外新建一个 Session（从检查点恢复会话的其他状态），压缩结果通过它写回检查点
    （/compact 用）；内存里已有的上下文不会被检查点里的旧内容覆盖。

    rails 要到第一次对话时才初始化，在那之前 ContextProcessorRail 还没把压缩器配置放进去；这时建出来的
    上下文不带压缩器，留在内存池里会让这个会话之后都不压缩，所以用完就移出内存池（loaded=False）。
    """
    react = agent.react_agent
    engine = react.context_engine
    context = engine.get_context(context_id=CONTEXT_ID, session_id=session_id)
    if context is not None and not fresh_session:
        yield OpenedContext(context)
        return
    from openjiuwen.core.session.agent import create_agent_session

    processors = processor_configs(agent)
    temporary = context is None and expect_compression and not processors
    session = create_agent_session(session_id=session_id, card=getattr(agent, "card", None))
    try:
        await session.pre_run()   # 不带 inputs：只从检查点恢复会话状态
        if context is None:
            if processors:
                context = await engine.create_context(session=session, processors=processors)
            else:
                context = await engine.create_context(session=session)
        yield OpenedContext(context, session, loaded=not temporary)
    finally:
        await session.close_stream()
        if temporary:
            await engine.clear_context(context_id=CONTEXT_ID, session_id=session_id)


def occupancy(context: Any, prompt_tokens: int = 0) -> tuple[int, bool]:
    """当前占用的 token 数，口径与 SDK 压缩器判断阈值时一致。返回 (tokens, 是否已含系统提示和工具定义)。

    有模型返回的用量时：最后一次用量 + 之后新增消息的估算（已含系统提示和工具定义）；
    没有时只能按消息估算，再加上 prompt_tokens（最近一次请求里系统提示、工具定义、技能的大小）。
    """
    messages = context.get_messages()
    try:
        from openjiuwen.core.context_engine.processor.forked.compressor.support.util import (
            count_usage_tokens_with_tail,
        )

        tokens = count_usage_tokens_with_tail(messages)
    except Exception:  # noqa: BLE001
        tokens = None
    if tokens is not None:
        return int(tokens), True
    return int(context.statistic().total_tokens) + prompt_tokens, prompt_tokens > 0


def estimate_tokens(context: Any) -> int:
    """逐条估算对话消息的 token 数（不用模型返回的用量）。压缩前后都用它，前后对比才是同一个口径：
    压缩后 SDK 会把旧用量标成过期，只能估算。"""
    messages = context.get_messages()
    try:
        from openjiuwen.core.context_engine.processor.forked.compressor.support.util import count_messages_tokens

        return int(count_messages_tokens(messages, context.token_counter()))
    except Exception:  # noqa: BLE001
        return sum(len(str(getattr(m, "content", "") or "")) // 3 for m in messages)


def usage_parts(snapshot: Optional[dict]) -> dict[str, int]:
    """context.usage 事件（每次请求前后 SDK 写进输出流）里各部分的 token 数。"""
    parts = (snapshot or {}).get("parts") or {}
    return {name: int((part or {}).get("tokens") or 0) for name, part in parts.items()}


def prompt_tokens_of(snapshot: Optional[dict]) -> int:
    parts = usage_parts(snapshot)
    return sum(parts.get(k, 0) for k in ("system_prompt", "tools", "skills"))


def processor_configs(agent: Any) -> list[tuple[str, Any]]:
    """ContextProcessorRail 放进 ReActAgent 配置里的处理器（名字, 配置）；rails 还没初始化时为空。"""
    config = getattr(getattr(agent, "react_agent", None), "config", None)
    return list(getattr(config, "context_processors", None) or [])


def trigger_ratio(agent: Any, expect_compression: bool = False) -> Optional[float]:
    """自动压缩的阈值（占窗口的比例），取处理器链里第一个按阈值触发的压缩器（DialogueCompressor）。

    rails 还没初始化（这次启动后还没发过消息）时按 SDK 的默认配置。没开压缩时返回 None。
    """
    for name, config in processor_configs(agent):
        ratio = getattr(config, "trigger_context_ratio", None)
        if name == "DialogueCompressor" and isinstance(ratio, (int, float)) and ratio > 0:
            return float(ratio)
    if not expect_compression:
        return None
    try:
        from openjiuwen.core.context_engine.processor.forked.compressor.dialogue_compressor import (
            DialogueCompressorConfig,
        )

        return float(DialogueCompressorConfig.model_fields["trigger_context_ratio"].default)
    except Exception:  # noqa: BLE001
        return None


def window_source(model_name: str, window: int) -> str:
    try:
        from openjiuwen.core.context_engine.context.context_utils import (
            DEFAULT_CONTEXT_MAX_TOKENS,
            MODEL_DEFAULT_CONTEXT_WINDOW_TOKENS,
        )
    except ImportError:
        return ""
    known = {str(k).lower(): v for k, v in MODEL_DEFAULT_CONTEXT_WINDOW_TOKENS.items()}.get((model_name or "").lower())
    if known == window:
        return "SDK 内置的模型表"
    if known is None and window == DEFAULT_CONTEXT_MAX_TOKENS:
        return f"默认值：{model_name} 不在 SDK 内置的模型表里"
    return "配置指定"


@dataclass
class ContextReport:
    tokens: int
    window: int
    window_note: str
    threshold: Optional[float]       # None：没有开自动压缩
    includes_prompt: bool            # tokens 是否已含系统提示和工具定义
    stats: dict[str, Any]
    parts: dict[str, int]            # 最近一次请求各部分的大小；这个进程里还没请求过时为空

    @property
    def ratio(self) -> float:
        return self.tokens / self.window if self.window > 0 else 0.0


async def context_report(agent: Any, model_name: str, session_id: str, snapshot: Optional[dict],
                         *, expect_compression: bool = True) -> ContextReport:
    async with opened_context(agent, session_id, expect_compression=expect_compression) as opened:
        tokens, includes_prompt = occupancy(opened.context, prompt_tokens_of(snapshot))
        usage = agent.get_context_usage(session_id, CONTEXT_ID)
        window = int(usage.get("context_window_tokens") or 0)
        return ContextReport(
            tokens=tokens,
            window=window,
            window_note=window_source(model_name, window),
            threshold=trigger_ratio(agent, expect_compression),
            includes_prompt=includes_prompt,
            stats=dict(usage.get("stats") or {}),
            parts=usage_parts(snapshot),
        )


# --------------------------------------------------------------------------- #
# 手动压缩
# --------------------------------------------------------------------------- #
@dataclass
class CompactOutcome:
    result: str          # compressed / noop / busy / empty / disabled / not_loaded（这次启动后还没发过消息）
    before: int = 0
    after: int = 0
    messages_before: int = 0
    messages_after: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved_ratio(self) -> float:
        return (self.before - self.after) / self.before if self.before > 0 else 0.0


async def compact(
    agent: Any,
    watcher: Optional[CompressionWatcher],
    session_id: str,
    instruction: str = "",
    *,
    on_start: Optional[Callable[[int, int], None]] = None,
) -> CompactOutcome:
    """主动压缩当前会话的上下文（与模型报超长时 SDK 走的是同一个入口），结果写回检查点。

    instruction：压缩时要特别保留的内容，传给 SDK 的 preserve_instruction。
    on_start(tokens, messages)：真正开始压缩前回调（终端先打出「正在压缩…」）。
    before / after 只算对话消息（与 SDK 压缩事件里的口径一致），不含系统提示和工具定义。
    """
    if watcher is None:
        return CompactOutcome("disabled")
    async with opened_context(agent, session_id, fresh_session=True, expect_compression=True) as opened:
        context = opened.context
        n_before = len(context.get_messages())
        if not n_before:
            return CompactOutcome("empty")
        if not opened.loaded:
            return CompactOutcome("not_loaded", messages_before=n_before)
        await watcher.listen()
        before = estimate_tokens(context)
        if on_start is not None:
            on_start(before, n_before)
        mark = watcher.error_seq
        kwargs = {"preserve_instruction": instruction} if instruction else {}
        result = await agent.react_agent.context_engine.compress_context(
            context_id=CONTEXT_ID, session=opened.session, **kwargs
        )
        code = result.get("result") if isinstance(result, dict) else result
        after = estimate_tokens(context)
        return CompactOutcome(str(code or "noop"), before, after, n_before, len(context.get_messages()),
                              watcher.errors_since(mark))

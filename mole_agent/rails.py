"""自定义 rails —— openJiuwen 里扩展 agent 行为的主要方式。

rail 是挂在 agent 生命周期上的钩子对象（before/after_model_call、
before/after_tool_call 等），priority 越大越先执行。这里演示几类典型用法：

- CommandGuardRail：硬拦截。bash 命令命中拒绝规则直接驳回，不执行、也不打扰用户。
- ApprovalRail：人工确认。继承 SDK 的 ConfirmInterruptRail，高危工具先中断等人确认，
  并支持「本会话总是允许」。
- ToolTraceRail：旁路观测。把工具调用写进输出流（给终端 UI 渲染）并落审计日志。
- TokenUsageRail：统计 token 用量。

before_tool_call 执行顺序：CommandGuardRail(95) → ApprovalRail(90) → ToolTraceRail(5)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.base import AgentInterrupt
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.rails import BaseInterruptRail, ConfirmInterruptRail
from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult

# rail 之间通过 ctx.extra 通信：记录被驳回的调用，供 ToolTraceRail 标记和审计
_REJECTED_KEY = "mole_rejected_calls"


def _call_id_of(ctx: Any, tool_call: Any = None) -> str:
    tc = tool_call if tool_call is not None else getattr(ctx.inputs, "tool_call", None)
    return str(getattr(tc, "id", "") or "")


def _mark_rejected(ctx: Any, tool_call: Any, by: str) -> None:
    ctx.extra.setdefault(_REJECTED_KEY, {})[_call_id_of(ctx, tool_call)] = by


# 与 SDK BashTool 相同的子命令切分方式：按 || && ; | 切开
_SHELL_OPERATOR_RE = re.compile(r"\s*(?:\|\||&&|[;|])\s*")


def split_shell_command(command: str) -> list[str]:
    return [part.strip() for part in _SHELL_OPERATOR_RE.split(command) if part.strip()]


def match_deny_rule(command: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    """返回命中的规则；未命中返回 None。逐个子命令检查（与 SDK BashTool 语义一致）。"""
    for segment in split_shell_command(command):
        for pattern in patterns:
            if pattern.search(segment):
                return pattern.pattern
    return None


class CommandGuardRail(BaseInterruptRail):
    """bash 命令黑名单。命中即驳回（RejectResult），模型会收到驳回原因并换方案。

    说明：SDK 的 BashTool 自带 deny_patterns，但只在 OPENJIUWEN_BASH_STRICT=1 时生效，
    所以这里用 rail 在执行层统一兜底。
    """

    priority = 95  # 高于 ApprovalRail：已经注定被拒的命令不必再问用户

    def __init__(self, deny_patterns: Iterable[str], tool_names: Iterable[str] = ("bash",)) -> None:
        super().__init__(tool_names=tool_names)
        self._patterns = [re.compile(p, re.IGNORECASE) for p in deny_patterns]

    async def resolve_interrupt(self, ctx, tool_call, user_input, auto_confirm_config=None):
        args = getattr(ctx.inputs, "tool_args", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"command": args}
        command = str((args or {}).get("command", "")) if isinstance(args, dict) else ""
        rule = match_deny_rule(command, self._patterns)
        if rule is None:
            return self.approve()
        _mark_rejected(ctx, tool_call, "guard")
        return self.reject(tool_result=(
            f"命令被安全规则拦截，未执行（规则：{rule}）。"
            "请改用更安全的做法；确实需要时，请把命令交给用户手动执行。"
        ))

CHUNK_TOOL_CALL = "tool_call"
CHUNK_TOOL_RESULT = "tool_result"


def _normalize_args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _result_to_text(result: Any) -> tuple[bool, str]:
    """把各种工具返回值统一成 (是否成功, 文本)。

    内置工具返回 ToolOutput(success, data, error)；@tool 函数返回 str/dict。
    """
    if result is None:
        return True, ""
    success = getattr(result, "success", True)
    error = getattr(result, "error", None)
    if success is False:
        return False, str(error or result)
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        for key in ("content", "output", "stdout", "message", "result"):
            if data.get(key):
                value = data[key]
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                return True, str(value)
    return True, str(result)


class ToolTraceRail(AgentRail):
    """把每次工具调用写成流式 chunk，并可选写入 JSONL 审计日志。"""

    priority = 5  # 很低：安全/确认类 rail 先裁决；等待确认中的调用不会出现，被驳回的会带 decision 标记

    def __init__(self, audit_path: Optional[Path] = None, project_dir: Optional[Path] = None) -> None:
        self._audit_path = audit_path
        self._project_dir = str(project_dir) if project_dir else None
        self._started: dict[str, float] = {}
        if audit_path:
            audit_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _call_id(inputs: Any) -> str:
        tool_call = getattr(inputs, "tool_call", None)
        return str(getattr(tool_call, "id", "") or id(inputs))

    async def before_tool_call(self, ctx: Any) -> None:
        inputs = ctx.inputs
        self._started[self._call_id(inputs)] = time.monotonic()
        if ctx.session is None:
            return
        await ctx.session.write_stream(OutputSchema(
            type=CHUNK_TOOL_CALL,
            index=0,
            payload={
                "tool_name": getattr(inputs, "tool_name", ""),
                "tool_args": _normalize_args(getattr(inputs, "tool_args", None)),
            },
        ))

    async def after_tool_call(self, ctx: Any) -> None:
        # after 钩子在 finally 里总会触发（含异常）。等待人工确认的中断不是失败：
        # 用户批准后同一个调用会重新走一遍，这里直接跳过，避免重复记录。
        if isinstance(ctx.exception, (AbortError, AgentInterrupt)):
            return
        inputs = ctx.inputs
        tool_name = getattr(inputs, "tool_name", "")
        tool_args = _normalize_args(getattr(inputs, "tool_args", None))
        ok, text = _result_to_text(getattr(inputs, "tool_result", None))
        if ctx.exception is not None:
            ok, text = False, f"{type(ctx.exception).__name__}: {ctx.exception}"
        decision = (ctx.extra.get(_REJECTED_KEY) or {}).pop(_call_id_of(ctx), None)
        if decision:
            ok = False
        decision = f"rejected_by_{decision}" if decision else "executed"
        started = self._started.pop(self._call_id(inputs), None)
        elapsed_ms = int((time.monotonic() - started) * 1000) if started else None

        if ctx.session is not None:
            await ctx.session.write_stream(OutputSchema(
                type=CHUNK_TOOL_RESULT,
                index=0,
                payload={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "ok": ok,
                    "decision": decision,
                    "text": text,
                    "elapsed_ms": elapsed_ms,
                },
            ))
        self._audit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project": self._project_dir,
            "tool": tool_name,
            "args": tool_args,
            "ok": ok,
            "decision": decision,
            "elapsed_ms": elapsed_ms,
            "result_preview": text[:500],
        })

    def _audit(self, record: dict[str, Any]) -> None:
        if not self._audit_path:
            return
        try:
            with self._audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # 审计失败不影响主流程


class TokenUsageRail(AgentRail):
    """累计每次模型调用的 token 用量。"""

    priority = 10

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.model_calls = 0

    async def after_model_call(self, ctx: Any) -> None:
        self.model_calls += 1
        response = getattr(ctx.inputs, "response", None)
        usage = getattr(response, "usage_metadata", None) or getattr(response, "usage", None)
        if usage is None:
            return
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0)

    def summary(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }


class ApprovalRail(ConfirmInterruptRail):
    """高危工具执行前中断，等待用户确认。

    用户回复 {"approved": true, "auto_confirm": true} 时，
    该工具在本会话内后续调用不再询问（/new 会清空）。
    """

    def __init__(self, tool_names: Optional[Iterable[str]] = None) -> None:
        super().__init__(tool_names=tool_names)
        self.always_allow: set[str] = set()

    async def resolve_interrupt(self, ctx, tool_call, user_input, auto_confirm_config=None):
        name = getattr(tool_call, "name", "") or getattr(ctx.inputs, "tool_name", "")
        if ctx.extra.get("_skip_tool"):
            return self.approve()  # 已被更高优先级的 rail 驳回，不会执行，无需再问
        if user_input is None and name in self.always_allow:
            return self.approve()
        if user_input is not None:
            get = user_input.get if isinstance(user_input, dict) else (lambda k: getattr(user_input, k, None))
            if get("approved") and get("auto_confirm"):
                self.always_allow.add(name)
        decision = await super().resolve_interrupt(ctx, tool_call, user_input, auto_confirm_config)
        if isinstance(decision, RejectResult):
            _mark_rejected(ctx, tool_call, "user")
        return decision

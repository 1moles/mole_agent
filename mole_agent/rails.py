"""自定义 rails —— openJiuwen 里扩展 agent 行为的主要方式。

rail 是挂在 agent 生命周期上的钩子对象（before/after_model_call、
before/after_tool_call 等），priority 越大越先执行。这里演示几类典型用法：

- CommandGuardRail：硬拦截。bash 命令命中拒绝规则直接驳回，不执行、也不打扰用户。
- ApprovalRail：人工确认。继承 SDK 的 ConfirmInterruptRail，高危工具先中断等人确认，
  并支持「本会话总是允许」。
- QuestionRail：模型调用 question 工具向用户提问时中断，等用户在终端回答后把答案作为工具结果交回。
- ReadOnlyShellRail：子 agent 专用，bash 只放行只读命令。
- ToolTraceRail：旁路观测。把工具调用写进输出流（给终端 UI 渲染）并落审计日志；
  子 agent 的调用经 ActivityFeed 转给终端。
- TokenUsageRail：统计 token 用量。
- CompressionRail：上下文压缩器的适配（只收流式的网关、记录压缩失败原因），见 context.py。

before_tool_call 执行顺序：CommandGuardRail(95) → ApprovalRail(90) → ToolTraceRail(5)
命令类工具（SHELL_TOOLS）：bash，以及 Windows 上的 powershell，安全 rail 对两者同样生效。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.base import AgentInterrupt
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.interrupt import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness.rails import BaseInterruptRail, ConfirmInterruptRail
from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult

from mole_agent.config import SHELL_TOOLS
from mole_agent.tools import question as question_tool

# rail 之间通过 ctx.extra 通信：记录被驳回的调用，供 ToolTraceRail 标记和审计
_REJECTED_KEY = "mole_rejected_calls"


def _call_id_of(ctx: Any, tool_call: Any = None) -> str:
    tc = tool_call if tool_call is not None else getattr(ctx.inputs, "tool_call", None)
    return str(getattr(tc, "id", "") or "")


def _mark_rejected(ctx: Any, tool_call: Any, by: str) -> None:
    ctx.extra.setdefault(_REJECTED_KEY, {})[_call_id_of(ctx, tool_call)] = by


# 与 SDK BashTool 相同的子命令切分方式：按 || && ; | 切开
_SHELL_OPERATOR_RE = re.compile(r"\s*(?:\|\||&&|[;|])\s*")
# 更严格的切分：再加上单个 &（bash 后台执行、cmd 的命令分隔符）和换行（多行命令）。
# 只用 SDK 的切法，`ls & sudo x`、`ls<换行>rm -rf x` 里的第二条命令会被漏检。
_STRICT_OPERATOR_RE = re.compile(r"\s*(?:\|\||&&|[;|&\r\n])\s*")


def split_shell_command(command: str) -> list[str]:
    return [part.strip() for part in _SHELL_OPERATOR_RE.split(command) if part.strip()]


def _strict_segments(command: str) -> list[str]:
    return [part.strip() for part in _STRICT_OPERATOR_RE.split(command) if part.strip()]


def match_deny_rule(command: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    """返回命中的规则；未命中返回 None。逐个子命令检查：SDK 的切法和更严格的切法各查一遍，只会多拦不会少拦。"""
    segments = split_shell_command(command)
    segments += [s for s in _strict_segments(command) if s not in segments]
    for segment in segments:
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

    def __init__(self, deny_patterns: Iterable[str], tool_names: Iterable[str] = SHELL_TOOLS) -> None:
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


class ActivityFeed:
    """子 agent 的活动转发给终端。

    task_tool 在内部消费子 agent 的输出流、只把最终答复交回主 agent（openjiuwen 0.1.18），
    子 agent 的工具调用不会出现在主 agent 的流里。子 agent 的 ToolTraceRail 把事件发到这里，
    终端订阅后就能显示子 agent 正在做什么。
    """

    def __init__(self) -> None:
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """返回取消订阅的函数。"""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def emit(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 —— 显示出错不能影响子 agent 执行
                pass


class ToolTraceRail(AgentRail):
    """把每次工具调用写成流式 chunk，并可选写入 JSONL 审计日志。"""

    priority = 5  # 很低：安全/确认类 rail 先裁决；等待确认中的调用不会出现，被驳回的会带 decision 标记

    def __init__(
        self,
        audit_path: Optional[Path] = None,
        project_dir: Optional[Path] = None,
        agent_name: str = "main",
        feed: Optional["ActivityFeed"] = None,
    ) -> None:
        self._audit_path = audit_path
        self._project_dir = str(project_dir) if project_dir else None
        self._agent_name = agent_name  # 审计里区分主 agent 与各个子 agent
        self._feed = feed              # 子 agent 用：输出流到不了终端，改走 ActivityFeed
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
        payload = {
            "tool_name": getattr(inputs, "tool_name", ""),
            "tool_args": _normalize_args(getattr(inputs, "tool_args", None)),
        }
        if self._feed is not None:
            self._feed.emit({"agent": self._agent_name, "type": CHUNK_TOOL_CALL, **payload})
        if ctx.session is None:
            return
        await ctx.session.write_stream(OutputSchema(type=CHUNK_TOOL_CALL, index=0, payload=payload))

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

        payload = {
            "tool_name": tool_name,
            "tool_args": tool_args,
            "ok": ok,
            "decision": decision,
            "text": text,
            "elapsed_ms": elapsed_ms,
        }
        if self._feed is not None:
            self._feed.emit({"agent": self._agent_name, "type": CHUNK_TOOL_RESULT, **payload})
        if ctx.session is not None:
            await ctx.session.write_stream(OutputSchema(type=CHUNK_TOOL_RESULT, index=0, payload=payload))
        self._audit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project": self._project_dir,
            "agent": self._agent_name,
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


class CompressionRail(AgentRail):
    """上下文压缩器的两处适配，细节见 context.py：

    - init：SDK 按 priority 从高到低初始化 rails，ContextProcessorRail(85) 先把压缩器配置放进 ReActAgent 的
      config.context_processors，这里排在它后面；网关只收流式时，把其中的 model_client 换成只走流式的客户端。
    - before_model_call：第一次调模型前订阅 SDK 的模型调用失败事件，记下压缩失败的原因（SDK 自己只写日志）。
    """

    priority = 50  # 必须低于 ContextProcessorRail(85)，init 时压缩器配置才已经在了

    def __init__(self, watcher: Any, client_config_wrapper: Optional[Callable[[Any], Any]] = None) -> None:
        super().__init__()
        self.watcher = watcher
        self.client_config_wrapper = client_config_wrapper

    def init(self, agent: Any) -> None:
        if self.client_config_wrapper is None:
            return
        config = getattr(getattr(agent, "react_agent", None), "config", None)
        for _, processor_config in getattr(config, "context_processors", None) or []:
            client_config = getattr(processor_config, "model_client", None)
            if client_config is not None:
                processor_config.model_client = self.client_config_wrapper(client_config)

    async def before_model_call(self, ctx: Any) -> None:
        await self.watcher.listen()


# 只读 shell：子 agent 没有交互入口，不能靠人工确认兜底，所以只放行明确只读的命令
_READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "pwd", "echo",
    "file", "stat", "du", "sort", "cut", "diff", "which", "basename", "dirname",
    "dir", "type", "tree", "where", "findstr",  # Windows cmd / PowerShell 里的只读命令
}
# 白名单命令里个别参数也能写文件或执行其他程序，单独拦掉
_WRITE_OR_EXEC_FLAGS = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "sort": ("-o", "--output"),
    "rg": ("--pre",),
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "status", "diff", "log", "show", "blame", "grep", "ls-files", "ls-tree", "rev-parse",
    "describe", "shortlog", "cat-file", "merge-base", "rev-list", "branch",
}
_GIT_BRANCH_WRITE_FLAGS = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u"}
_GIT_WRITE_OR_EXEC_FLAGS = ("--output", "-O", "--open-files-in-pager", "--ext-diff")
_HARMLESS_REDIRECTS = (
    "&>/dev/null", "2>/dev/null", ">/dev/null", "2>&1",   # POSIX
    "2>nul", "2>NUL", ">nul", ">NUL", "2>$null",          # Windows cmd / PowerShell
)


_SINGLE_AMP_RE = re.compile(r"(?<!&)&(?!&)")


def _command_name(word: str) -> str:
    """/usr/bin/git、C:\\Git\\bin\\git.exe、GIT → git（Windows 上命令名不区分大小写）。"""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def read_only_violation(command: str, *, windows: bool = False) -> Optional[str]:
    """返回命令不是只读的原因；是只读命令时返回 None。宁可误拒，不可误放。

    windows=True：命令可能交给 PowerShell / cmd 执行（Windows 上 bash 工具会按命令自动选择），
    额外拒绝括号和花括号——PowerShell 里 (...) / {...} 会执行其中的命令。
    """
    text = command.strip()
    if not text:
        return "空命令"
    for harmless in _HARMLESS_REDIRECTS:
        text = text.replace(harmless, " ")
    for token, why in ((">", "输出重定向会写文件"), ("`", "命令替换"), ("$(", "命令替换"), ("<(", "进程替换")):
        if token in text:
            return why
    if _SINGLE_AMP_RE.search(text):  # && 可以；单个 & 在 bash 里是后台执行，在 cmd 里是命令分隔符
        return "单个 & 会在后台或另起一条命令执行"
    if windows and any(ch in text for ch in "(){}"):
        return "Windows 上命令可能由 PowerShell 执行，括号里的内容会被当成命令运行"
    for segment in _strict_segments(text):  # 换行也算命令分隔
        words = segment.split()
        if not words:
            continue
        if "=" in words[0]:
            return f"不允许设置环境变量：{words[0]}"
        base = _command_name(words[0])
        if base == "git":
            args = words[1:]
            while args and args[0] in {"-C", "--no-pager"}:  # 跳过全局选项（-c 可改配置，不放行）
                args = args[2:] if args[0] == "-C" else args[1:]
            sub = args[0] if args else ""
            if sub not in _READ_ONLY_GIT_SUBCOMMANDS:
                return f"git {sub or '(无子命令)'} 不是只读操作"
            if sub == "branch" and any(a in _GIT_BRANCH_WRITE_FLAGS for a in args[1:]):
                return "git branch 带了修改分支的参数"
            if any(a.startswith(_GIT_WRITE_OR_EXEC_FLAGS) for a in args[1:]):
                return "git 参数会写文件或调用外部程序"
            continue
        if base not in _READ_ONLY_COMMANDS:
            return f"{base} 不在只读命令白名单里"
        risky = _WRITE_OR_EXEC_FLAGS.get(base, ())
        if any(w == f or w.startswith(f + "=") for w in words[1:] for f in risky):
            return f"{base} 带了写文件或执行程序的参数"
    return None


class ReadOnlyShellRail(BaseInterruptRail):
    """只读子 agent 的命令守卫：只放行白名单里的只读命令，其余直接驳回。

    子 agent 由 task_tool 在内部运行，它触发的人工确认不会传到用户终端（openjiuwen 0.1.18），
    所以子 agent 不挂 ApprovalRail，而是用这个 rail 把命令限制成只读：
    - bash：按白名单检查；显式指定 shell_type=powershell / cmd 的一律拒绝
    - powershell（只在 Windows 上注册）：一律拒绝，PowerShell 语法太灵活，没法可靠地判断只读
    """

    priority = 95  # 与 CommandGuardRail 同级：先于任何可能执行的环节

    def __init__(self, tool_names: Iterable[str] = SHELL_TOOLS, *, windows: Optional[bool] = None) -> None:
        super().__init__(tool_names=tool_names)
        self._windows = (os.name == "nt") if windows is None else windows

    def violation(self, tool_name: str, args: Any) -> Optional[str]:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"command": args}
        args = args if isinstance(args, dict) else {}
        if tool_name != "bash":
            return f"只读子 agent 不能使用 {tool_name}"
        shell = str(args.get("shell_type") or "auto").lower()
        if shell not in {"auto", "bash", "sh"}:
            return f"只读子 agent 不能指定 shell_type={shell}"
        return read_only_violation(str(args.get("command", "")), windows=self._windows)

    async def resolve_interrupt(self, ctx, tool_call, user_input, auto_confirm_config=None):
        name = getattr(tool_call, "name", "") or getattr(ctx.inputs, "tool_name", "") or "bash"
        reason = self.violation(name, getattr(ctx.inputs, "tool_args", None))
        if reason is None:
            return self.approve()
        _mark_rejected(ctx, tool_call, "guard")
        return self.reject(tool_result=(
            f"只读子 agent 不能执行这条命令（{reason}）。请改用 read_file / grep / glob / git_changes，"
            "或在最终报告里建议主 agent 去执行。"
        ))


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


class QuestionRequest(InterruptRequest):
    """question 工具的中断请求：带上整理过（补齐默认值）的问题，终端据此显示。"""

    questions: list[dict] = []


class QuestionRail(BaseInterruptRail):
    """拦下 question 工具：先中断等用户回答，恢复后把答案作为工具结果交回模型（工具本身不执行）。

    参数不合法时不打扰用户，直接把错误说明作为工具结果返回，让模型改好再问。
    """

    priority = 90  # 与 ApprovalRail 同级；只处理 question，二者拦截的工具不重叠

    def __init__(self) -> None:
        super().__init__(tool_names=[question_tool.NAME])

    async def resolve_interrupt(self, ctx, tool_call, user_input, auto_confirm_config=None):
        questions, error = question_tool.normalize_questions(getattr(tool_call, "arguments", None))
        if error:
            return self.reject(tool_result=f"Error: {error}")
        if user_input is not None:
            answers = question_tool.parse_answers(user_input, questions)
            if answers is not None:
                return self.reject(tool_result=question_tool.format_answers(questions, answers))
        return self.interrupt(QuestionRequest(questions=[q.to_dict() for q in questions]))

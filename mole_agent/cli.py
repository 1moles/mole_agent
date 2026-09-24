"""终端交互：流式渲染、人工确认 / 反问、斜杠命令、Ctrl+C 中断。

用法：
    mole                      # 在当前目录启动 REPL
    mole -p "解释一下这个仓库"  # 单次执行后退出
    mole --project ~/code/foo  # 指定项目目录
    mole --check              # 只检查配置并 ping 一次模型
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import signal
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel

from mole_agent import __version__
from mole_agent.config import Settings, load_settings
from mole_agent.models import ModelChoice, ModelSelectionError, list_remote_models, save_last_model

console = Console(highlight=False)

HELP_TEXT = """\
[bold]斜杠命令[/bold]
  /model [序号|供应商/模型]  切换模型（不带参数时列出可选模型），对话上下文保留
  /models [供应商]   在线查询供应商当前可用的模型
  /review [范围或要求]  代码检视（只读）：默认未提交改动，也可以 /review 提交 a1b2c3d、/review 和 main 比
  /new               开启新会话（清空上下文和「总是允许」记录）
  /history [序号]    查看当前项目的历史会话，选序号看详情（看完可以按 r 接着聊）
  /resume [序号]     回到某个历史会话继续聊（恢复完整上下文）；启动时 mole -c 继续最近的会话
  /usage             查看本会话 token 用量
  /help              显示帮助
  /exit              退出
[bold]其他[/bold]
  Ctrl+C             打断正在执行的任务
  Ctrl+D             退出
  项目根目录放一个 MOLE.md（或 AGENTS.md / CLAUDE.md）写项目约定，启动时自动加载
  子 agent（explore_agent / code_reviewer）由主 agent 按需派活，终端里以 │ 开头的行是它们的动作
"""

# 有 code_reviewer 子 agent 时交给它：检视读的 diff 和上下文不进主对话
REVIEW_BY_SUBAGENT_PROMPT = """\
请用 task_tool 调用 code_reviewer 子代理做代码检视，task_description 写：「代码检视。用户的要求：{request}」。
拿到报告后完整转述给我，不要删改其中的问题和结论；不要修改任何文件。
"""

REVIEW_PROMPT = """\
请对当前仓库未提交的改动做一次代码检视。
如果技能列表里有代码检视技能（例如 code-review），先用 skill_tool 读取它并严格按技能执行，忽略下面的通用步骤；
没有的话按以下步骤：
1. 先调用 git_changes 获取改动；需要上下文时用 read_file / grep 查看相关代码。
2. 按「正确性 > 安全性 > 兼容性（与存量代码/接口） > 可维护性 > 性能」排序列出问题，
   每条写明 `路径:行号`、问题描述、影响、修改建议。没有问题的维度不用硬凑。
3. 只做检视，不要修改任何文件。
4. 最后给出结论：建议合入 / 修改后合入 / 不建议合入。
"""


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _short(text: Any, limit: int = 100) -> str:
    s = str(text).replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _format_args(tool_name: str, args: Any) -> str:
    if tool_name == "task_tool" and isinstance(args, dict):
        return _short(f"{args.get('subagent_type', '?')} · {args.get('task_description', '')}", 90)
    if isinstance(args, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "url"):
            if args.get(key):
                extra = ""
                if key == "pattern" and args.get("path"):
                    extra = f", {args['path']}"
                return _short(f"{args[key]}{extra}", 90)
        if not args:
            return ""
        return _short(json.dumps(args, ensure_ascii=False), 90)
    return _short(args or "", 90)


def configure_sdk_logging(settings: Settings) -> None:
    """SDK 默认把日志打到控制台和 ./logs；这里改为只写 ~/.mole-agent/logs，避免刷屏和污染项目目录。"""
    level = "INFO" if settings.verbose else "WARNING"
    try:
        from openjiuwen.core.common.logging.default.constant import DEFAULT_INNER_LOG_CONFIG
        from openjiuwen.core.common.logging.log_config import configure_log_config

        settings.log_dir.mkdir(parents=True, exist_ok=True)
        cfg = copy.deepcopy(DEFAULT_INNER_LOG_CONFIG)
        cfg.update(
            level=level,
            log_path=str(settings.log_dir) + "/",
            output=["file"],
            interface_output=["file"],
            performance_output=["file"],
        )
        configure_log_config(cfg)
    except Exception:  # noqa: BLE001 —— 日志配置失败不应阻止启动
        pass
    # 部分 SDK 模块直接用标准库 logging.getLogger(__name__)，没有 handler 时会落到 stderr；
    # 统一接到文件，且不向根 logger 传播，保证终端只显示 agent 的输出。
    sdk_logger = logging.getLogger("openjiuwen")
    sdk_logger.setLevel(logging.INFO if settings.verbose else logging.WARNING)
    sdk_logger.propagate = False
    if not any(isinstance(h, logging.FileHandler) for h in sdk_logger.handlers):
        try:
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(settings.log_dir / "sdk.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            sdk_logger.addHandler(handler)
        except OSError:
            sdk_logger.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------- #
# 流式渲染
# --------------------------------------------------------------------------- #
@dataclass
class Pending:
    interaction_id: str
    request: Any


@dataclass
class StreamRenderer:
    verbose: bool = False
    pending: list[Pending] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    # 按出现顺序记下回复文本和工具结果，写进会话历史：("assistant", 文本) / ("tool", payload)
    transcript: list[tuple[str, Any]] = field(default_factory=list)
    _segment: list[str] = field(default_factory=list)
    _in_text: bool = False
    _saw_llm_output: bool = False

    def _flush_segment(self) -> None:
        if self._segment:
            self.transcript.append(("assistant", "".join(self._segment)))
            self._segment = []

    def _end_text(self) -> None:
        self._flush_segment()
        if self._in_text:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._in_text = False

    def take_transcript(self) -> list[tuple[str, Any]]:
        """取走目前为止的记录（中断时也能拿到已输出的部分）。"""
        self._flush_segment()
        items, self.transcript = self.transcript, []
        return items

    def _write_text(self, text: str) -> None:
        if not text:
            return
        if not self._in_text:
            console.print("●", style="bright_green", end=" ")  # 交给 rich 上色：旧版 Windows 控制台不认裸 ANSI
            self._in_text = True
        sys.stdout.write(text)
        sys.stdout.flush()
        self.text_parts.append(text)
        self._segment.append(text)

    def feed(self, chunk: Any) -> None:
        ctype = _attr(chunk, "type", "")
        payload = _attr(chunk, "payload")

        if ctype == "llm_output":
            self._saw_llm_output = True
            self._write_text(_attr(payload, "content", "") if isinstance(payload, dict) else str(payload or ""))
            return
        if ctype == "answer":
            # answer 与 llm_output 内容重复，只在没收到流式文本时兜底显示
            if not self._saw_llm_output:
                content = payload
                if isinstance(payload, dict):
                    content = payload.get("output") or payload.get("content") or ""
                    if isinstance(content, dict):
                        content = content.get("output") or content.get("content") or ""
                self._write_text(str(content or ""))
            return

        self._end_text()

        if ctype == "llm_reasoning":
            if self.verbose:
                text = _attr(payload, "content", "") if isinstance(payload, dict) else str(payload or "")
                console.print(f"[dim italic]{esc(text)}[/dim italic]", end="")
        elif ctype == "tool_call":
            name = _attr(payload, "tool_name", "")
            args = _format_args(name, _attr(payload, "tool_args"))
            console.print(f"[cyan]● {esc(name)}[/cyan]" + (f"[dim]({esc(args)})[/dim]" if args else ""))
        elif ctype == "tool_result":
            self._render_tool_result(payload)
        elif ctype == "message":
            text = _attr(payload, "content", "") if isinstance(payload, dict) else str(payload or "")
            if text:
                console.print(f"[dim]  ⚙ {esc(text)}[/dim]")
        elif ctype == "controller_output":
            raw = str(payload)
            if "task_failed" in raw.lower():
                console.print(f"[red]✗ 任务失败：{esc(_short(raw, 300))}[/red]")
        elif ctype == "__interaction__":
            if isinstance(payload, dict):
                iid, value = payload.get("interaction_id", "unknown"), payload
            else:
                iid, value = _attr(payload, "id", "unknown"), _attr(payload, "value", payload)
            self.pending.append(Pending(interaction_id=iid, request=value))
        elif self.verbose and ctype:
            console.print(f"[dim]  · {esc(ctype)}: {esc(_short(payload, 120))}[/dim]")

    def feed_subagent(self, event: dict[str, Any]) -> None:
        """子 agent 的工具调用（经 ActivityFeed 转来），缩进显示在 task_tool 下面。"""
        self._end_text()
        agent, name = event.get("agent", "?"), event.get("tool_name", "")
        if event.get("type") == "tool_call":
            args = _format_args(name, event.get("tool_args"))
            console.print(f"[dim]  │ {esc(agent)} ● {esc(name)}" + (f"({esc(args)})" if args else "") + "[/dim]")
        elif str(event.get("decision", "")).startswith("rejected"):
            console.print(f"  │   ⊘ {_short(event.get('text', ''), 140)}", style="yellow", markup=False)
        elif not event.get("ok", True):
            console.print(f"  │   ✗ {_short(event.get('text', ''), 140)}", style="red", markup=False)

    def _render_tool_result(self, payload: Any) -> None:
        self.transcript.append(("tool", payload))
        name = _attr(payload, "tool_name", "")
        ok = _attr(payload, "ok", True)
        text = str(_attr(payload, "text", "") or "")
        elapsed = _attr(payload, "elapsed_ms")
        lines = [line for line in text.splitlines() if line.strip()]
        if lines and lines[0].startswith("Command:"):  # bash 结果首行是回显的命令，已在上一行展示过
            lines = lines[1:]
        suffix = f" · {elapsed}ms" if elapsed is not None else ""
        if str(_attr(payload, "decision", "")).startswith("rejected"):
            console.print(f"  ⎿ ⊘ {_short(text, 160)}", style="yellow", markup=False)
            return
        if not ok:
            console.print(f"  ⎿ {_short(text, 160)}", style="red", markup=False, end="")
            console.print(f"[dim]{suffix}[/dim]")
            return
        if name == "task_tool" and ok:
            console.print(f"[dim]  ⎿ 子 agent 完成，报告 {len(lines)} 行{suffix}[/dim]")
            return
        if name.startswith("todo_") and lines:
            for line in lines[:12]:
                console.print(f"[dim]  ⎿ {esc(line)}[/dim]")
            return
        head = _short(lines[0], 100) if lines else "（无输出）"
        more = f" (+{len(lines) - 1} 行)" if len(lines) > 1 else ""
        console.print(f"[dim]  ⎿ {esc(head)}{more}{suffix}[/dim]")

    def finish(self) -> str:
        self._end_text()
        return "".join(self.text_parts)


# --------------------------------------------------------------------------- #
# REPL
# --------------------------------------------------------------------------- #
class Repl:
    def __init__(self, settings: Settings, bundle: Any) -> None:
        self.settings = settings
        self.bundle = bundle
        self.session_id = self._new_session_id()
        settings.home_dir.mkdir(parents=True, exist_ok=True)
        self._prompt: Any = None
        self._answer_prompt: Any = None
        self.history: Any = None  # history.HistoryRecorder；MOLE_SAVE_HISTORY=false 时为 None
        self.checkpoint: Any = None  # history.ContextCheckpoint，由 _amain 打开；为 None 时不能续聊
        self.checkpoint_error = "没有开启会话历史（MOLE_SAVE_HISTORY=false）" if not settings.save_history else ""
        if settings.save_history:
            try:
                from mole_agent.history import HistoryRecorder

                self.history = HistoryRecorder(settings)
                self.history.start(self.session_id, settings.model_spec)
            except Exception as exc:  # noqa: BLE001 —— 历史记录是辅助功能，失败不影响使用
                console.print(f"[yellow]⚠ 会话历史不可用：{esc(str(exc))}[/yellow]")
                self.history = None

    # 输入框按需创建：非终端环境（测试、管道）下不需要 prompt_toolkit
    @property
    def prompt(self) -> Any:
        if self._prompt is None:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory

            self._prompt = PromptSession(history=FileHistory(str(self.settings.history_path)))
        return self._prompt

    @property
    def answer_prompt(self) -> Any:
        if self._answer_prompt is None:
            from prompt_toolkit import PromptSession

            self._answer_prompt = PromptSession()
        return self._answer_prompt

    @staticmethod
    def _new_session_id() -> str:
        return f"mole-{uuid.uuid4().hex[:8]}"

    # ---------- 一轮对话（含中断-恢复循环） ----------
    async def run_turn(self, text: str) -> str:
        from openjiuwen.core.runner import Runner

        query: Any = text
        final = ""
        while True:
            renderer = StreamRenderer(verbose=self.settings.verbose)
            unsubscribe = self.bundle.activity.subscribe(renderer.feed_subagent)
            try:
                stream = Runner.run_agent_streaming(self.bundle.agent, {"query": query}, session=self.session_id)
                async for chunk in stream:
                    renderer.feed(chunk)
            finally:
                unsubscribe()
                self._record(renderer.take_transcript())
            final = renderer.finish() or final
            if not renderer.pending:
                return final
            query = await self._collect_answers(renderer.pending)
            if query is None:
                return final

    def _record(self, transcript: list[tuple[str, Any]]) -> None:
        if self.history is None:
            return
        for kind, item in transcript:
            if kind == "assistant":
                self.history.assistant(item)
            else:
                name = str(_attr(item, "tool_name", ""))
                self.history.tool(
                    name, _format_args(name, _attr(item, "tool_args")), bool(_attr(item, "ok", True)),
                    str(_attr(item, "decision", "")), str(_attr(item, "text", "") or ""),
                )

    async def _ask(self, message: str) -> str:
        return (await self.answer_prompt.prompt_async(message)).strip()

    async def _collect_answers(self, pending: list[Pending]) -> Any:
        from openjiuwen.core.session import InteractiveInput

        interactive = InteractiveInput()
        for item in pending:
            req = item.request
            tool_name = _attr(req, "tool_name", "")
            if tool_name == "ask_user":
                interactive.update(item.interaction_id, await self._answer_ask_user(req))
            else:
                interactive.update(item.interaction_id, await self._answer_approval(tool_name, req))
        return interactive

    async def _answer_ask_user(self, req: Any) -> dict[str, Any]:
        questions = _attr(req, "questions") or []
        console.print("\n[bold magenta]? 需要你的输入[/bold magenta]")
        if not questions:
            args = _attr(req, "tool_args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            question = (args.get("query") if isinstance(args, dict) else None) or _attr(req, "message", "")
            console.print(f"  {question}", markup=False)
            return {"answer": await self._ask("回答 › ")}

        answers: dict[str, str] = {}
        for i, q in enumerate(questions, 1):
            header = _attr(q, "header", f"Q{i}")
            text = _attr(q, "question", "")
            options = _attr(q, "options") or []
            console.print(f"\n[cyan]{esc(header)}[/cyan] {esc(text)}")
            for j, opt in enumerate(options, 1):
                desc = _attr(opt, "description", "")
                console.print(f"  [dim]{j}.[/dim] {esc(str(_attr(opt, 'label', '')))}" + (f" [dim]- {esc(str(desc))}[/dim]" if desc else ""))
            ans = await self._ask("回答（可填序号或自由输入） › ")
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                ans = str(_attr(options[int(ans) - 1], "label", ans))
            answers[text] = ans
        return {"answers": answers}

    async def _answer_approval(self, tool_name: str, req: Any) -> dict[str, Any]:
        args = _attr(req, "tool_args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        console.print(f"\n[bold yellow]⚠ 请求执行 {esc(tool_name)}[/bold yellow]")
        if isinstance(args, dict):
            for k, v in args.items():
                val = str(v)
                if len(val) > 600:
                    val = val[:600] + f"…(共 {len(val)} 字符)"
                console.print(f"  {k}: {val}", style="dim", markup=False)
        elif args:
            console.print(f"  {args}", style="dim", markup=False)
        ans = await self._ask("允许？[Y]是 / [a]本会话总是允许 / [n]否（也可直接输入拒绝理由） › ")
        low = ans.lower()
        if low in {"", "y", "yes", "ok", "是"}:
            return {"approved": True, "feedback": "", "auto_confirm": False}
        if low in {"a", "always", "总是"}:
            console.print(f"[dim]  本会话内 {esc(tool_name)} 将不再询问（/new 重置）[/dim]")
            return {"approved": True, "feedback": "", "auto_confirm": True}
        feedback = "" if low in {"n", "no", "否"} else ans
        return {"approved": False, "feedback": f"用户拒绝执行。{feedback}".strip(), "auto_confirm": False}

    # ---------- 斜杠命令 ----------
    async def handle_slash(self, text: str) -> str | None:
        """处理斜杠命令；返回要发给 agent 的文本，或 None 表示已处理完。"""
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()
        if cmd in {"/exit", "/quit", "/q"}:
            raise EOFError
        if cmd == "/help":
            console.print(HELP_TEXT)
        elif cmd == "/new":
            self.session_id = self._new_session_id()
            if self.bundle.approval:
                self.bundle.approval.always_allow.clear()
            self.bundle.usage.reset()
            if self.history is not None:
                self.history.start(self.session_id, self.settings.model_spec)
            console.print(f"[dim]已开启新会话 {self.session_id}[/dim]")
        elif cmd == "/history":
            await self._cmd_history(rest.strip())
        elif cmd == "/resume":
            await self._cmd_resume(rest.strip())
        elif cmd == "/usage":
            s = self.bundle.usage.summary()
            console.print(
                f"[dim]当前模型 {esc(self.settings.model_spec)} · 模型调用 {s['model_calls']} 次 · "
                f"输入 {s['input_tokens']} · 输出 {s['output_tokens']} · 合计 {s['total_tokens']} tokens（含子 agent）[/dim]"
            )
        elif cmd == "/model":
            await self._cmd_model(rest.strip())
        elif cmd == "/models":
            await self._cmd_models(rest.strip())
        elif cmd == "/review":
            return review_prompt(rest.strip(), "code_reviewer" in self.bundle.subagents)
        else:
            console.print(f"[red]未知命令 {esc(cmd)}，输入 /help 查看[/red]")
        return None

    # ---------- 模型切换 ----------
    async def _cmd_model(self, arg: str) -> None:
        if arg:
            choices = usable_choices(self.settings)
        else:
            choices = print_model_menu(self.settings)
            arg = await self._ask("输入序号或 供应商/模型 切换（回车取消） › ")
            if not arg:
                return
        try:
            if arg.isdigit():
                idx = int(arg)
                if not 1 <= idx <= len(choices):
                    raise ModelSelectionError(f"序号超出范围：1-{len(choices)}")
                choice = choices[idx - 1]
            else:
                choice = self.settings.catalog.resolve(arg)
            self.switch_to(choice)
        except Exception as exc:  # noqa: BLE001 —— 切换失败保持原模型
            console.print(f"[red]✗ 切换失败：{esc(str(exc))}[/red]")
            return
        console.print(f"[green]✓ 已切换到 {esc(choice.spec)}[/green][dim]（对话上下文保留，下次启动默认使用它）[/dim]")

    def switch_to(self, choice: ModelChoice) -> None:
        from mole_agent.agent import switch_model

        switch_model(self.bundle, self.settings, choice)
        save_last_model(self.settings.state_path, choice.spec)
        if self.history is not None:
            self.history.system(f"切换模型：{choice.spec}")

    # ---------- 会话历史与续聊 ----------
    async def _pick_session(self, arg: str, action: str) -> Any:
        """按序号 / 会话 id 前缀选一个历史会话；不带参数时列出来让用户选。返回 SessionSummary 或 None。"""
        from mole_agent.history import list_sessions

        items = list_sessions(self.history.dir, limit=HISTORY_LIST_LIMIT)
        if not items:
            console.print("[dim]当前项目还没有历史会话[/dim]")
            return None
        if not arg:
            print_history_list(items, self.session_id, self.settings.project_dir.name)
            arg = await self._ask(f"输入序号{action}（回车返回） › ")
            if not arg:
                return None
        if arg.isdigit():
            if not 1 <= int(arg) <= len(items):
                console.print(f"[red]序号超出范围：1-{len(items)}[/red]")
                return None
            return items[int(arg) - 1]
        hits = [s for s in items if s.id.startswith(arg)]  # 也可以直接写会话 id（或能唯一确定它的前缀）
        if len(hits) != 1:
            console.print(f"[red]{'找不到' if not hits else '有多个'}以 {esc(arg)} 开头的会话[/red]")
            return None
        return hits[0]

    async def _cmd_history(self, arg: str) -> None:
        if self.history is None:
            console.print("[dim]没有开启会话历史（MOLE_SAVE_HISTORY=false）[/dim]")
            return
        from mole_agent.history import load_session

        target = await self._pick_session(arg, "查看详情")
        if target is None:
            return
        session = load_session(self.history.dir, target.id)
        if session is None:
            console.print(f"[red]读取会话 {esc(target.id)} 失败（文件不存在或已损坏）[/red]")
            return
        current = session.session_id == self.session_id
        print_session_detail(session, current=current)
        if current or self.checkpoint is None or not await self.checkpoint.exists(session.session_id):
            return
        if (await self._ask("输入 r 从这里继续聊（回车返回） › ")).lower() in {"r", "resume", "继续"}:
            await self.resume(session.session_id)

    async def _cmd_resume(self, arg: str) -> None:
        if self.history is None:
            console.print("[dim]没有开启会话历史（MOLE_SAVE_HISTORY=false），不能续聊[/dim]")
            return
        target = await self._pick_session(arg, "继续聊")
        if target is not None:
            await self.resume(target.id)

    async def resume(self, session_id: str) -> bool:
        """切换到历史会话：之后的对话用它的会话 id，SDK 从检查点里恢复完整上下文。"""
        from mole_agent.history import load_session

        if session_id == self.session_id:
            console.print("[dim]已经在这个会话里了[/dim]")
            return True
        if self.checkpoint is None:
            console.print(f"[yellow]续聊不可用：{esc(self.checkpoint_error or '检查点没有打开')}[/yellow]")
            return False
        if not await self.checkpoint.exists(session_id):
            console.print("[yellow]这个会话没有保存对话上下文（多半是开启续聊之前的会话），只能用 /history 查看[/yellow]")
            return False
        stored = load_session(self.history.dir, session_id) if self.history is not None else None
        self.session_id = session_id
        if self.bundle.approval:
            self.bundle.approval.always_allow.clear()  # 「总是允许」只对当前这次会话有效，不跟着历史会话走
        self.bundle.usage.reset()
        if self.history is not None and stored is not None:
            self.history.resume(stored)
            self.history.system(f"继续会话（模型 {self.settings.model_spec}）")
        print_resume_summary(stored, session_id, self.settings.model_spec)
        return True

    async def continue_latest(self) -> bool:
        """mole -c：继续当前项目最近一个能续聊的会话。"""
        from mole_agent.history import list_sessions

        if self.history is None or self.checkpoint is None:
            console.print(f"[yellow]续聊不可用：{esc(self.checkpoint_error or '检查点没有打开')}，已开启新会话[/yellow]")
            return False
        for item in list_sessions(self.history.dir):
            if item.id != self.session_id and await self.checkpoint.exists(item.id):
                return await self.resume(item.id)
        console.print("[dim]当前项目没有可以继续的会话，已开启新会话[/dim]")
        return False

    async def _cmd_models(self, arg: str) -> None:
        name = arg or self.settings.provider_name
        provider = self.settings.catalog.providers.get(name)
        if provider is None:
            console.print(f"[red]没有名为 {esc(name)} 的供应商[/red]")
            return
        console.print(f"[dim]正在查询 {esc(name)} 的模型列表…[/dim]")
        try:
            ids = await list_remote_models(provider)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗ 查询失败（部分供应商不提供 /models 接口）：{esc(_short(exc, 200))}[/red]")
            return
        if not ids:
            console.print("[dim]接口没有返回模型[/dim]")
            return
        for i in range(0, len(ids), 3):
            console.print("  " + "".join(f"{m:<40}" for m in ids[i:i + 3]), markup=False)
        console.print(f"[dim]共 {len(ids)} 个。切换：/model {esc(name)}/<模型名>；常用的可加到 models.toml 的 models 列表[/dim]")

    # ---------- 带 Ctrl+C 中断的执行 ----------
    async def run_interruptible(self, text: str, label: str | None = None) -> None:
        """label：写进历史的用户输入（斜杠命令展开成长提示词时，记录用户实际敲的命令）。"""
        if self.history is not None:
            self.history.user(label or text)
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(self.run_turn(text))
        restore = _cancel_on_sigint(loop, task)
        try:
            await task
        except asyncio.CancelledError:
            console.print("\n[dim]⏹ 已中断[/dim]")
            if self.history is not None:
                self.history.system("已中断")
            try:
                await self.bundle.agent.abort()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]✗ {esc(_friendly_error(exc))}[/red]")
            if self.history is not None:
                self.history.system(f"出错：{_friendly_error(exc)}")
            if self.settings.verbose:
                console.print_exception()
        finally:
            restore()

    async def loop(self) -> None:
        while True:
            try:
                text = (await self.prompt.prompt_async("› ")).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not text:
                continue
            typed = text
            if text.startswith("/"):
                try:
                    text = await self.handle_slash(text)
                except EOFError:
                    break
                if not text:
                    continue
            await self.run_interruptible(text, label=typed)
            console.print()
        console.print("[dim]再见～[/dim]")


HISTORY_LIST_LIMIT = 30
_ROLE_STYLE = {"user": "bold cyan", "assistant": "", "tool": "dim", "system": "dim yellow"}


def print_history_list(items: list[Any], current_id: str, project_name: str) -> None:
    from mole_agent.history import local_time

    note = f"，只显示最近 {HISTORY_LIST_LIMIT} 个" if len(items) >= HISTORY_LIST_LIMIT else ""
    console.print(f"[bold]历史会话[/bold] [dim]{esc(project_name)} · 最近活动的在前{note}[/dim]")
    for i, item in enumerate(items, 1):
        mark = "  [green]← 当前[/green]" if item.id == current_id else ""
        console.print(
            f"  [dim]{i:>2}.[/dim] {esc(local_time(item.updated_at))}  [dim]{item.user_turns:>2} 轮 · "
            f"{esc(item.model)}[/dim]  {esc(item.title)}{mark}"
        )


def print_session_detail(session: Any, current: bool = False) -> None:
    from mole_agent.history import local_time

    users = sum(1 for m in session.messages if m.role == "user")
    title = f"{session.session_id} · {local_time(session.created_at)} · {session.model} · {users} 轮"
    console.rule(esc(title + ("（当前会话）" if current else "")), style="dim")
    for message in session.messages:
        text = message.content.rstrip()
        if message.role == "user":
            console.print()
            console.print(f"› {local_time(message.timestamp, '%H:%M')}", style="dim", end=" ")
            console.print(text, style=_ROLE_STYLE["user"], markup=False)
        elif message.role == "assistant":
            console.print("●", style="bright_green", end=" ")
            console.print(text, markup=False)
        elif message.role == "tool":
            console.print(f"  ⎿ {text}", style=_ROLE_STYLE["tool"], markup=False)
        else:
            console.print(f"  · {text}", style=_ROLE_STYLE.get(message.role, "dim"), markup=False)
    console.rule(style="dim")


def print_resume_summary(stored: Any, session_id: str, current_model: str) -> None:
    """回到历史会话时，把最后一问一答亮出来，提醒聊到哪儿了。"""
    if stored is None:
        console.print(f"[green]✓ 已回到会话 {esc(session_id)}[/green][dim]（对话上下文已恢复）[/dim]")
        return
    users = [m for m in stored.messages if m.role == "user"]
    replies = [m for m in stored.messages if m.role == "assistant"]
    note = f"，原来用的是 {stored.model}，现在用 {current_model}" if stored.model and stored.model != current_model else ""
    console.print(f"[green]✓ 已回到会话 {esc(session_id)}[/green][dim] · {len(users)} 轮{esc(note)} · 对话上下文已恢复[/dim]")
    if users:
        console.print(f"  [dim]上次问：[/dim]{esc(_short(users[-1].content, 120))}")
    if replies:
        console.print(f"  [dim]上次答：[/dim]{esc(_short(replies[-1].content, 120))}")
    console.print("[dim]  如果上次停在等你确认的操作上，接着聊时会先重新问你[/dim]")


def _cancel_on_sigint(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> Any:
    """执行期间 Ctrl+C 只取消当前任务、回到输入框；返回恢复原处理方式的函数。

    macOS / Linux 用事件循环的 add_signal_handler。Windows 的事件循环不支持它，而 asyncio.run
    默认会在 Ctrl+C 时取消整个程序，所以改用 signal.signal 临时接管，任务结束后再还原。
    """
    try:
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        return lambda: loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, RuntimeError):
        pass
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(task.cancel))
    except ValueError:  # 不在主线程（例如被嵌入到别的程序里）：保持默认行为
        return lambda: None
    return lambda: signal.signal(signal.SIGINT, previous)


def review_prompt(request: str, has_reviewer: bool) -> str:
    if has_reviewer:
        return REVIEW_BY_SUBAGENT_PROMPT.format(request=request or "检视当前未提交的改动")
    return REVIEW_PROMPT + (f"\n补充要求：{request}" if request else "")


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "401" in low or "authentication" in low or "api key" in low:
        return f"鉴权失败，请检查 .env 里该供应商的 API key，或 models.toml 里 headers 引用的环境变量：{msg}"
    if "429" in low or ("rate" in low and "limit" in low):
        return f"触发限流，稍后再试：{msg}"
    if "timeout" in low or "timed out" in low:
        return f"请求超时，检查网络或调大 MOLE_TIMEOUT：{msg}"
    if "context" in low and ("length" in low or "too long" in low):
        return f"上下文超长，试试 /new 开新会话：{msg}"
    return f"{type(exc).__name__}: {msg}"


def usable_choices(settings: Settings) -> list[ModelChoice]:
    """可直接选择的模型（已配置 key 的供应商下 models 列表里的模型），顺序即菜单编号。"""
    return [
        ModelChoice(p, m)
        for p in settings.catalog.providers.values() if not p.problems()
        for m in p.models
    ]


def print_model_menu(settings: Settings) -> list[ModelChoice]:
    """列出可用模型并编号，返回与编号对应的列表。没配 key 的供应商只汇总成一行，不参与编号。"""
    catalog = settings.catalog
    choices: list[ModelChoice] = []
    if catalog.is_empty():
        console.print("[dim]还没有配置模型供应商：复制 models.example.toml 为 models.toml 后填写[/dim]")
        return choices
    current = settings.model_spec
    console.print(f"[bold]当前模型[/bold] {esc(current or '（无）')}")
    unavailable: list[str] = []
    for provider in catalog.providers.values():
        if provider.problems():
            missing = provider.missing_env()
            unavailable.append(f"{provider.name}({'、'.join(missing)})" if missing
                               else f"{provider.name}（{provider.problems()[0]}）")
            continue
        console.print(f"\n[cyan]{esc(provider.name)}[/cyan] [dim]{esc(provider.client_provider)} · {esc(provider.api_base)}[/dim]")
        if not provider.models:
            console.print(f"  [dim]（未列出模型：/models {esc(provider.name)} 在线查询，或 /model {esc(provider.name)}/<模型名>）[/dim]")
        for model in provider.models:
            choice = ModelChoice(provider, model)
            choices.append(choice)
            mark = "  [green]← 当前[/green]" if choice.spec == current else ""
            console.print(f"  [dim]{len(choices):>2}.[/dim] {esc(model)}{mark}")
    if current and not any(c.spec == current for c in choices):
        console.print(f"\n[dim]（当前模型 {esc(current)} 不在列表里，是用 供应商/模型 直接指定的）[/dim]")
    if unavailable:
        console.print(f"\n[dim]暂不可用的供应商（在 .env 里设置括号中的环境变量即可使用）：{esc('、'.join(unavailable))}[/dim]")
    return choices


def _print_banner(settings: Settings, bundle: Any) -> None:
    confirm = ", ".join(settings.confirm_tools) or "无（全部自动执行）"
    mcp = ", ".join(bundle.mcp_names) or "无"
    subagents = "、".join(f"{name}（{model}）" for name, model in bundle.subagents.items()) or "无"
    body = (
        f"[bold]{esc(settings.agent_name)}[/bold] v{__version__} · openJiuwen DeepAgent\n"
        f"[dim]模型[/dim]  {esc(settings.model_spec)} ({esc(settings.provider)})  [dim]/model 切换[/dim]\n"
        f"[dim]项目[/dim]  {esc(str(settings.project_dir))}\n"
        f"[dim]确认[/dim]  {esc(confirm)}\n"
        f"[dim]MCP [/dim]  {esc(mcp)}\n"
        f"[dim]子代理[/dim] {esc(subagents)}\n"
        f"[dim]输入 /help 查看命令，Ctrl+C 打断，Ctrl+D 退出[/dim]"
    )
    console.print(Panel(body, border_style="green", expand=False))
    for warning in bundle.warnings:
        console.print(f"[yellow]⚠ {esc(warning)}[/yellow]")


def describe_auth(settings: Settings) -> str:
    """只显示请求头的名字，不显示取值（取值通常是密钥）。"""
    names = "、".join(settings.custom_headers) or "（无）"
    if settings.auth == "headers":
        return f"请求头 {names}" + ("，另带 Authorization: Bearer" if settings.api_key else "，不发 Authorization")
    if settings.auth == "none":
        return "不鉴权" + (f"（附加请求头 {names}）" if settings.custom_headers else "")
    return "API key（Authorization: Bearer）" + (f"，附加请求头 {names}" if settings.custom_headers else "")


async def _explain_failure(settings: Settings, exc: BaseException) -> None:
    """SDK 的报错只有最外层一句话；这里把异常链打出来，连接类错误再逐步做网络诊断。"""
    from mole_agent import netcheck

    chain = netcheck.describe_exception_chain(exc)
    if len(chain) > 1:
        console.print("[dim]异常链（最后一行是根因）：[/dim]")
        for line in chain[1:]:
            console.print(f"  {line}", style="dim", markup=False)
    if not netcheck.is_connection_error(exc):
        return
    console.print("[bold]网络诊断[/bold]（按 openjiuwen 的实际行为逐步检查）")
    steps = await asyncio.to_thread(netcheck.diagnose, settings.api_base, settings.verify_ssl)
    for step in steps:
        style = {True: "green", False: "red", None: "dim"}[step.ok]
        console.print(f"  {step.render()}", style=style, markup=False)


async def _check(settings: Settings) -> int:
    from mole_agent.agent import build_model

    console.print(f"模型：{settings.model_spec} @ {settings.api_base} ({settings.provider})", markup=False)
    console.print(f"鉴权：{describe_auth(settings)}", markup=False)
    if settings.stream_only:
        console.print("调用：只走流式（stream_only = true，非流式调用在本地拼接）", markup=False)
    sources = "、".join(str(p) for p in settings.catalog.sources) or "（无 models.toml，用的是 .env 里的 MOLE_*）"
    console.print(f"配置：{sources}", markup=False)
    try:
        reply = await build_model(settings).invoke([{"role": "user", "content": "只回复两个字：你好"}])
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ 模型调用失败：{esc(_friendly_error(exc))}[/red]")
        await _explain_failure(settings, exc)
        return 1
    console.print(f"[green]✓ 模型可用[/green]，回复：{esc(_short(getattr(reply, 'content', reply), 80))}")
    return 0


async def _amain(args: argparse.Namespace) -> int:
    settings = load_settings(args.project, verbose=args.verbose, model=args.model)
    if args.list_models:
        print_model_menu(settings)
        return 0
    if args.yes:
        settings.confirm_tools = []
    problems = settings.validate()
    if problems:
        console.print("[red]配置不完整：[/red]\n  - " + "\n  - ".join(problems))
        if settings.catalog.is_empty():
            console.print("[dim]复制 models.example.toml 为 models.toml、.env.example 为 .env 后填写[/dim]")
        else:
            console.print("[dim]mole --list-models 查看已配置的模型；mole -m 供应商/模型 指定其他模型[/dim]")
        return 2

    configure_sdk_logging(settings)
    if args.check:
        return await _check(settings)

    from openjiuwen.core.runner import Runner

    from mole_agent.agent import build_agent

    bundle = build_agent(settings)
    checkpoint = None
    repl = Repl(settings, bundle)
    if settings.save_history:
        from mole_agent.history import open_context_checkpoint

        checkpoint, reason = await open_context_checkpoint(settings)
        repl.checkpoint, repl.checkpoint_error = checkpoint, reason
        if checkpoint is None:
            console.print(f"[yellow]⚠ {esc(reason)}[/yellow]")
    await Runner.start()
    try:
        if not args.prompt:
            _print_banner(settings, bundle)
        if args.resume:
            await repl._cmd_resume("" if args.resume is True else args.resume)
        elif args.continue_:
            await repl.continue_latest()
        if args.prompt:
            await repl.run_interruptible(args.prompt)
            return 0
        await repl.loop()
        return 0
    finally:
        await Runner.stop()
        if checkpoint is not None:
            await checkpoint.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mole", description="基于 openJiuwen 的终端编码助手")
    parser.add_argument("-p", "--prompt", help="单次执行这条指令后退出")
    parser.add_argument("--project", help="项目目录（默认当前目录）")
    parser.add_argument("-m", "--model", help="使用的模型：供应商/模型、供应商 或 模型名（见 models.toml）")
    parser.add_argument("--list-models", action="store_true", help="列出 models.toml 里配置的供应商和模型")
    parser.add_argument("-y", "--yes", action="store_true", help="所有工具自动执行，不再询问（慎用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示思考过程与 SDK 日志")
    parser.add_argument("--check", action="store_true", help="检查配置并 ping 一次模型（可配合 -m）")
    parser.add_argument("-c", "--continue", dest="continue_", action="store_true",
                        help="继续当前项目最近的一个会话（恢复完整上下文）")
    parser.add_argument("-r", "--resume", nargs="?", const=True, default=None, metavar="序号|会话id",
                        help="选一个历史会话继续聊；不带参数时列出来选")
    parser.add_argument("--version", action="version", version=f"mole-agent {__version__}")
    args = parser.parse_args()
    # Windows 上输出被重定向（管道、文件）时默认是 GBK 编码，遇到它编不了的字符会直接崩；改成替换掉
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(asyncio.run(_amain(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

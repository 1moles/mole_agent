"""组装 agent：模型 + 系统提示词 + 工具 + rails → openJiuwen DeepAgent。

分层关系（从下往上）：
    openjiuwen.core      模型客户端 / 工具 / Session / Runner / ReAct 循环
    openjiuwen.harness   DeepAgent：任务循环、上下文工程、子智能体、技能、工作区
    mole_agent           本项目：人设、自定义工具、子 agent、自定义 rails、终端交互
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    LLMAuthMode,
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails import SkillUseRail, SysOperationRail
from openjiuwen.harness.workspace.workspace import Workspace

from mole_agent.config import AGENT_ID, Settings
from mole_agent.context import CompressionWatcher, extend_overflow_detection
from mole_agent.models import ModelChoice, ModelSelectionError
from mole_agent.prompts import build_system_prompt
from mole_agent.rails import (
    ActivityFeed, ApprovalRail, CommandGuardRail, CompressionRail, QuestionRail, TokenUsageRail, ToolTraceRail,
)
from mole_agent.tools import build_custom_tools

log = logging.getLogger("mole_agent")


@dataclass
class AgentBundle:
    agent: Any                    # openjiuwen.harness.DeepAgent
    usage: TokenUsageRail
    approval: ApprovalRail | None
    mcp_names: list[str]
    subagents: dict[str, str] = field(default_factory=dict)   # 子 agent 名 → 使用的模型（显示用）
    activity: ActivityFeed = field(default_factory=ActivityFeed)  # 子 agent 的工具调用，终端订阅显示
    warnings: list[str] = field(default_factory=list)
    compression: CompressionWatcher | None = None  # 上下文压缩器的适配与失败记录；没开压缩时为 None


async def collect_stream(chunks: Any, output_parser: Any = None) -> AssistantMessage:
    """把流式分片拼成一条完整回复，行为与 SDK 的非流式 invoke 一致（解析失败时 parser_content 为 None）。"""
    merged: Optional[AssistantMessageChunk] = None
    async for chunk in chunks:
        if isinstance(chunk, AssistantMessageChunk):
            merged = chunk if merged is None else merged + chunk
    if merged is None:
        return AssistantMessage(content="", tool_calls=[])
    fields = {k: getattr(merged, k) for k in AssistantMessage.model_fields if hasattr(merged, k)}
    fields["tool_calls"] = merged.tool_calls or []
    if "parser_content" in AssistantMessage.model_fields:
        fields["parser_content"] = None
    message = AssistantMessage(**fields)
    if output_parser is not None and message.content:
        try:
            message.parser_content = await output_parser.parse(message.content)
        except Exception:  # noqa: BLE001
            log.warning("流式拼接后的回复解析失败（output_parser=%s）", output_parser)
    return message


class StreamOnlyModel(Model):
    """给只接受流式请求（stream=true）的模型网关用：invoke 也走流式，在本地把分片拼成完整回复。

    agent 主循环本来就用 stream()；但 SDK 里还有不少地方用 invoke()（任务完成判断、图片能力探测，
    以及 mole --check），非流式请求会被这类网关拒绝。上下文压缩器不用这个对象，它按配置自己建模型客户端，
    见 stream_only_client_config。
    注意不要把 stream=True 写进 ModelRequestConfig：它会被原样塞进 invoke 的请求参数，
    OpenAI SDK 于是返回流对象，而 invoke 按完整回复去解析，报 'AsyncStream' object has no attribute 'choices'。
    """

    async def invoke(self, messages, *, output_parser=None, **kwargs) -> AssistantMessage:
        return await collect_stream(self.stream(messages, **kwargs), output_parser)


# --------------------------------------------------------------------------- #
# 只走流式的模型客户端（给上下文压缩器用）
# --------------------------------------------------------------------------- #
# 压缩器按 ContextProcessorRail 放进 ReActAgent 配置里的 model_client（ModelClientConfig）自己建 Model，
# 调的是非流式 invoke。在 SDK 的客户端注册表里登记一个 client_provider，把这份配置换成它：
# 建出来的客户端里面还是 SDK 按原配置建的客户端，只是 invoke 改成流式拼接。全程只用公开接口。
STREAM_ONLY_PROVIDER = "mole_stream_only"
_WRAPPED_CLIENT_CONFIGS: dict[str, ModelClientConfig] = {}   # 包装后配置的 client_id → 原配置


class StreamOnlyClient:
    """包在 SDK 模型客户端外面：stream 原样转发，invoke 用 stream 拼成完整回复，其余属性都转给里面的客户端。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        async for chunk in self.inner.stream(*args, **kwargs):
            yield chunk

    async def invoke(self, *args: Any, output_parser: Any = None, **kwargs: Any) -> AssistantMessage:
        return await collect_stream(self.stream(*args, **kwargs), output_parser)


def _register_stream_only_client() -> None:
    from openjiuwen.core.common.clients import get_client_registry

    registry = get_client_registry()
    if f"llm_{STREAM_ONLY_PROVIDER}" in registry.list_clients():
        return

    @registry.register_client(STREAM_ONLY_PROVIDER, client_type="llm")
    def create(model_config: Any = None, model_client_config: Any = None, **_: Any) -> StreamOnlyClient:
        from openjiuwen.core.foundation.llm.model_clients import create_model_client

        original = _WRAPPED_CLIENT_CONFIGS[model_client_config.client_id]
        return StreamOnlyClient(create_model_client(client_config=original, model_config=model_config))


def stream_only_client_config(original: ModelClientConfig) -> ModelClientConfig:
    """同一份客户端配置，换成只走流式的客户端。已经换过的原样返回。"""
    if original.client_provider == STREAM_ONLY_PROVIDER:
        return original
    _register_stream_only_client()
    wrapped = original.model_copy(update={"client_provider": STREAM_ONLY_PROVIDER})
    _WRAPPED_CLIENT_CONFIGS[wrapped.client_id] = original
    return wrapped


# models.toml 的 auth → openjiuwen 的鉴权方式；api_key 是 SDK 默认值，不显式传
_AUTH_MODES = {"headers": LLMAuthMode.CustomHeaders, "none": LLMAuthMode.NoneAuth}


def build_model(settings: Settings):
    """等价于 init_model(...)，多支持鉴权方式和「只走流式」两个选项。

    init_model 没有 auth_mode 参数，请求头鉴权（auth = "headers"）要直接构造 ModelClientConfig：
    auth_mode=custom_headers 且没有 key 时，SDK 不发 Authorization，只发 custom_headers。
    """
    client_kwargs: dict[str, Any] = dict(
        client_provider=settings.provider,
        api_key=settings.api_key,
        api_base=settings.api_base,
        timeout=settings.timeout,
        max_retries=3,  # 与 init_model 的默认值一致
        verify_ssl=settings.verify_ssl,
        custom_headers=dict(settings.custom_headers) or None,
    )
    if settings.auth in _AUTH_MODES:
        client_kwargs["auth_mode"] = _AUTH_MODES[settings.auth]
    model_cls = StreamOnlyModel if settings.stream_only else Model
    return model_cls(
        model_client_config=ModelClientConfig(**client_kwargs),
        model_config=ModelRequestConfig(
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        ),
    )


def switch_model(bundle: "AgentBundle", settings: Settings, choice: ModelChoice) -> None:
    """运行中切换 供应商/模型，保留当前会话上下文。

    只替换 DeepAgent 内层 ReActAgent 的 LLM 与模型元数据（全部走公开接口：
    react_agent / set_llm / config / update_model_context），不重建 agent，
    所以对话历史、rails、工具、「总是允许」记录都保持不变。
    注意：上下文压缩（ContextProcessorRail）在启动时固定了所用模型，切换后仍用启动时的模型做压缩
    （是否只走流式也按启动时的供应商）。
    """
    problems = choice.provider.problems()
    if problems:
        raise ModelSelectionError("；".join(problems))

    trial = copy.copy(settings)  # 先在副本上构建，失败不影响当前配置
    trial.apply_choice(choice)
    model = build_model(trial)

    agent = bundle.agent
    agent.deep_config.model = model
    react = agent.react_agent
    if react is not None:
        react.set_llm(model)
        cfg = react.config
        updates = {
            "model_name": choice.model,
            "model_client_config": model.model_client_config,
            "model_config_obj": model.model_config,
            "model_provider": trial.provider,
            "api_base": trial.api_base,
            "api_key": trial.api_key,
        }
        for key, value in updates.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
    agent.update_model_context(model_name=choice.model)
    settings.apply_choice(choice)


def load_mcp_configs(settings: Settings) -> list[McpServerConfig]:
    """读取 .mcp.json（项目级优先，其次 ~/.mole-agent/mcp.json），格式兼容 Claude Code：

    {"mcpServers": {"name": {"command": "npx", "args": [...], "env": {}}           # stdio
                   "name2": {"type": "sse" | "streamable-http", "url": "http://..."}}}
    """
    configs: list[McpServerConfig] = []
    seen: set[str] = set()
    for path in settings.mcp_config_paths:
        if not path.is_file():
            continue
        try:
            servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
        except (OSError, ValueError) as exc:
            log.warning("忽略无法解析的 MCP 配置 %s: %s", path, exc)
            continue
        for name, spec in servers.items():
            if name in seen or not isinstance(spec, dict) or spec.get("disabled"):
                continue
            transport = spec.get("type") or spec.get("transport") or ("stdio" if spec.get("command") else "sse")
            if transport == "http":  # Claude Code 里 http 指 streamable HTTP
                transport = "streamable-http"
            params = {k: spec[k] for k in ("command", "args", "env", "cwd") if spec.get(k) is not None}
            configs.append(McpServerConfig(
                server_id=f"mole_mcp_{name}",
                server_name=name,
                server_path=spec.get("url", ""),
                client_type=transport,
                params=params,
                auth_headers=spec.get("headers", {}) or {},
            ))
            seen.add(name)
    return configs


def _symlink_targets(root: Path, max_depth: int = 3) -> list[Path]:
    """技能目录里符号链接指向的真实目录（例如链接到 ~/.claude/skills 下的技能）。"""
    targets: list[Path] = []
    if not root.is_dir():
        return targets
    base_depth = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                targets.append(entry.resolve())
    return targets


def sandbox_roots(settings: Settings) -> list[Path]:
    """文件/命令沙箱允许访问的目录：项目目录、agent 工作区、技能目录（含其中符号链接的目标）。

    SDK 默认的沙箱只有 [工作区, 项目目录]，放在 ~/.mole-agent/skills 或软链接进来的技能
    会因为在沙箱外而读不到，所以这里显式把技能目录也加进去。
    """
    roots: list[Path] = [settings.project_dir.resolve(), settings.workspace_dir.resolve()]
    for skills_dir in settings.skills_dirs:
        roots.append(skills_dir.expanduser().resolve())
        roots.extend(_symlink_targets(skills_dir.expanduser()))
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def build_sys_operation(settings: Settings) -> Optional[SysOperation]:
    """创建带自定义沙箱的本地 SysOperation；不限制项目目录时交给 SDK 默认创建。"""
    if not settings.restrict_to_project:
        return None
    roots = [str(r) for r in sandbox_roots(settings)]
    digest = hashlib.sha1("\n".join(roots).encode("utf-8")).hexdigest()[:10]
    sysop_id = f"{AGENT_ID}_sysop_{digest}"  # 沙箱不同则 id 不同，避免复用到旧配置
    existing = Runner.resource_mgr.get_sys_operation(sysop_id)
    if existing is not None:
        return existing
    card = SysOperationCard(
        id=sysop_id,
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=None, restrict_to_sandbox=True, sandbox_root=roots),
    )
    result = Runner.resource_mgr.add_sys_operation(card)
    if result.is_err():
        log.warning("创建 sys_operation 失败，改用 SDK 默认沙箱：%s", result.msg())
        return None
    return Runner.resource_mgr.get_sys_operation(sysop_id)


def build_subagent_configs(
    settings: Settings,
    sys_operation: Optional[SysOperation],
    usage: TokenUsageRail,
    activity: ActivityFeed,
) -> tuple[list[Any], dict[str, str], list[str]]:
    """扫描 mole_agent/subagents/，返回 (SubAgentConfig 列表, {名字: 模型}, 警告)。"""
    if not settings.enable_subagents:
        return [], {}, []
    from mole_agent.subagents import SubagentEnv, build_subagents

    env = SubagentEnv(
        settings=settings, build_model=build_model, sys_operation=sys_operation,
        usage_rail=usage, feed=activity,
    )
    configs = build_subagents(env)
    names = {c.agent_card.name: env.model_label(c.agent_card.name) for c in configs}
    return configs, names, env.warnings


def build_agent(settings: Settings) -> AgentBundle:
    model = build_model(settings)
    lang = settings.language
    sys_operation = build_sys_operation(settings)  # 沙箱 = 项目目录 + 工作区 + 技能目录，子 agent 共用
    activity = ActivityFeed()

    usage = TokenUsageRail()
    trace = ToolTraceRail(
        audit_path=settings.audit_log_path if settings.audit_log else None,
        project_dir=settings.project_dir,
    )
    rails: list[Any] = [
        # 注册 read_file / write_file / edit_file / glob / grep / list_files / bash
        # （bash_deny_patterns 仅在 OPENJIUWEN_BASH_STRICT=1 时由 SDK 生效，常态由 CommandGuardRail 兜底）
        SysOperationRail(bash_deny_patterns=settings.bash_deny_patterns),
        CommandGuardRail(settings.bash_deny_patterns),  # 危险命令硬拦截
        QuestionRail(),         # 拦下 question 工具（tools/question.py）：中断等用户回答，代替 SDK 的 ask_user
        SkillUseRail(           # 加载 SKILL.md 技能（目录不存在会被跳过）
            skills_dir=[str(p) for p in settings.skills_dirs],
            skill_mode="all",
            include_tools=False,  # 文件/命令工具已由 SysOperationRail 提供
        ),
        usage,
        trace,
    ]

    approval: ApprovalRail | None = None
    if settings.confirm_tools:
        approval = ApprovalRail(tool_names=settings.confirm_tools)
        rails.append(approval)

    compression: CompressionWatcher | None = None
    if settings.enable_context_rails:
        try:  # 上下文窗口管理：长对话自动压缩，模型报超长时压缩后重试（见 context.py）
            from openjiuwen.harness.rails.context_engineer import ContextAssembleRail, ContextProcessorRail
        except ImportError:
            log.info("当前 openjiuwen 版本没有 context_engineer rails，已跳过")
        else:
            compression = CompressionWatcher(stream_only=settings.stream_only)
            wrapper = stream_only_client_config if settings.stream_only else None  # 压缩用启动时的模型，按它判断
            rails += [ContextProcessorRail(preset=True), ContextAssembleRail(), CompressionRail(compression, wrapper)]

    tools: list[Any] = build_custom_tools(settings)
    if settings.enable_web:
        from openjiuwen.harness.tools import create_web_tools
        tools += create_web_tools(language=lang, agent_id=AGENT_ID)

    mcps = load_mcp_configs(settings)
    subagents, subagent_models, warnings = build_subagent_configs(settings, sys_operation, usage, activity)

    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(root_path=str(settings.workspace_dir), language=lang)

    agent = create_deep_agent(
        model,
        card=AgentCard(id=AGENT_ID, name=AGENT_ID, description=f"{settings.agent_name} 编码助手"),
        system_prompt=build_system_prompt(settings, subagents=list(subagent_models)),
        tools=tools,
        mcps=mcps or None,
        subagents=subagents or None,  # 非空时 SDK 自动挂 SubagentRail，注册 task_tool
        rails=rails,
        enable_task_loop=True,        # 外层任务循环：模型说「做完了」之前持续推进
        enable_task_planning=True,    # todo 规划工具
        max_iterations=settings.max_iterations,
        workspace=workspace,          # agent 私有工作区：~/.mole-agent/workspace
        restrict_to_work_dir=settings.restrict_to_project,
        sys_operation=sys_operation,
        language=lang,
        # 以下两个字段透传给 DeepAgentConfig：shell 在项目目录执行、相对路径以项目为基准
        cwd=str(settings.project_dir),
        project_root=str(settings.project_dir),
    )
    if compression is not None and agent.react_agent is not None:
        extend_overflow_detection(agent.react_agent.context_engine)
    return AgentBundle(
        agent=agent, usage=usage, approval=approval, mcp_names=[c.server_name for c in mcps],
        subagents=subagent_models, activity=activity, warnings=warnings, compression=compression,
    )

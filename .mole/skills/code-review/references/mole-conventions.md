# Mole 项目约定（检视时逐条对照）

违反下面任一条，至少记为「建议」；标了「阻塞」的直接记为阻塞项。

## 分层与依赖

- 模块职责：`config.py` 配置 → `models.py` 供应商/模型目录 → `prompts.py` 系统提示词 →
  `tools/` 自定义工具 → `rails.py` 自定义 rails → `agent.py` 组装 DeepAgent → `cli.py` 终端交互。
  新代码放进职责对应的模块，不要在 `cli.py` 里写业务逻辑。
- `config.py`、`models.py`、`prompts.py` 不在模块顶层 import `openjiuwen`；`cli.py` 只在函数内部
  import SDK。这样 `mole --help`、`--list-models` 和配置报错不需要加载整个 SDK。
- 新增第三方依赖必须写进 `pyproject.toml` 的 `dependencies`，并说明用途。

## 使用 openjiuwen SDK

- 只用公开接口：`create_deep_agent`、`DeepAgent.react_agent` / `deep_config` / `update_model_context`、
  `ReActAgent.set_llm` / `config`、`AgentRail` 钩子、`BaseInterruptRail` 的 `approve` / `reject` / `interrupt`。
  **阻塞**：业务代码访问 SDK 的 `_` 开头属性（只允许出现在 `tests/`）。
- 要兼容 PyPI 发布版 0.1.18.x。开发分支才有的接口不能用（例如
  `openjiuwen.core.single_agent.ability_manager.resolve_tool_result_text`）。不确定时查
  `.venv/lib/python3.*/site-packages/openjiuwen/` 里的源码。
- SDK 的已知缺陷要在代码里注释原因，例如 `pyproject.toml` 里显式依赖 `opentelemetry-sdk`、
  bash 拒绝规则由 `CommandGuardRail` 执行（SDK 的 `deny_patterns` 只在 `OPENJIUWEN_BASH_STRICT=1` 时生效）。

## Rails

- 每个 rail 都要显式定义 `priority`，并在注释里说明为什么排在谁前后。当前顺序：
  `CommandGuardRail(95) > ApprovalRail(90) > TokenUsageRail(10) > ToolTraceRail(5)`。
- `DeepAgentRail` 子类的 `__init__` 必须调用 `super().__init__()`。
- 拦截工具调用用 `BaseInterruptRail.resolve_interrupt` 返回 `approve()` / `reject()` / `interrupt()`，
  不要在钩子里直接抛异常或手改 `ctx.extra["_skip_tool"]`。
- `after_tool_call` 在 finally 里总会触发：等待人工确认的中断（`AbortError` / `AgentInterrupt`）
  不能记成失败或写进审计。
- rail 之间通过 `ctx.extra` 通信时，要按 tool_call id 区分（并行工具调用共享同一个 `ctx.extra`）。

## 自定义工具（`mole_agent/tools/`）

- 一个工具一个文件：`tools/<工具名>.py`，文件名与工具名一致；模块提供 `create(settings) -> Tool`，
  可选 `enabled(settings) -> bool`。靠自动发现注册，**不要**在 `__init__.py` 或 `agent.py` 里手工加列表。
- 以下划线开头的文件不会被加载：共用函数放 `_common.py`，且只放至少两个工具会用到的东西；
  新工具从 `_template.py` 复制，模板本身要保持可运行（有测试覆盖）。
- 真正干活的逻辑写成同步函数（便于测试），`create` 里的异步包装只负责 `asyncio.to_thread` 调用它。
- 用 `tool(func, name=..., description=..., input_params={JSON Schema})`；`description` 要写清
  「什么时候该用」，参数要有默认值和说明。
- 阻塞 IO（文件遍历、子进程）放进 `asyncio.to_thread`，不能直接在协程里跑。
- 路径参数必须经过 `_common.resolve_inside` 限制在项目目录内。**阻塞**：能读写项目外路径的工具。
- 只读工具不得有任何写操作；输出要设上限，用 `_common.truncate` 截断并提示。
- `subprocess` 必须带 `timeout`，用参数列表而不是 `shell=True` 拼字符串。

## 安全

- **阻塞**：放宽默认安全策略——`restrict_to_project` 默认值、`MOLE_CONFIRM_TOOLS` 默认列表、
  删除或削弱 `DEFAULT_BASH_DENY` 规则。
- 新增或修改 bash 拒绝规则时，`tests/test_offline.py` 的「应拦截」和「应放行」两张用例表都要补用例
  （防止误拦正常命令，例如 `make -f Makefile`、`rm -rf build/`）。
- **阻塞**：API key 出现在 `models.toml`、日志、审计日志、异常信息或终端输出里。key 只能来自环境变量 / `.env`。
- 审计日志（`audit.jsonl`）里的工具结果要截断（`result_preview`）。

## 终端输出（`cli.py`）

- **阻塞**：`console.print` 的 markup 字符串里直接插入动态文本（模型输出、工具结果、文件路径、
  用户输入、异常信息）。必须用 `esc()` 转义或传 `markup=False`，否则内容里的 `[...]` 会被当成
  rich 标记，轻则样式错乱，重则抛 `MarkupError`。
- SDK 日志只能写到 `~/.mole-agent/logs`，不能输出到终端，也不能在项目目录生成 `logs/`。

## 配置与兼容

- Mole 自身的配置项统一用 `MOLE_` 前缀，必须有默认值，并同步写进 `.env.example` 和 README。
- 供应商、模型相关配置走 `models.toml`；`.env` 里旧的 `MOLE_PROVIDER` / `MOLE_API_BASE` /
  `MOLE_API_KEY` / `MOLE_MODEL` 写法必须继续可用。
- 命令行参数、斜杠命令、`state.json` 格式的变更要兼容旧用法，或在 README 里写明迁移方式。

## 模型切换

- `switch_model` 只通过公开接口替换 LLM 和模型元数据，**不重建 agent**（重建会丢失对话上下文和
  「总是允许」记录）。
- 切换失败时当前 `Settings` 和 agent 不能被改动（先在副本上构建新模型）。

## 测试

- 测试不联网、不需要真实 API key；需要模型时用 `tests/test_e2e_fake_llm.py` 里的 `ScriptedModel`。
- 用 `monkeypatch` 隔离 `PACKAGE_ROOT`、`MOLE_HOME` 和相关环境变量，不能读到开发者真实的
  `.env`、`models.toml`、`state.json`。
- 新功能、修复的 bug 都要有对应测试；断言要验证行为本身，而不是 mock 被调用了。

## 代码风格

- 面向 Python 3.11–3.13；公开函数写类型注解。
- 注释和文档用中文，解释「为什么」，不复述代码。
- 面向用户的行为变更同步更新 README。

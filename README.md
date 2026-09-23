# Mole：基于 openJiuwen agent-core 的终端编码助手

在任意项目目录里运行 `mole`，用自然语言让它读代码、改代码、跑命令、做检视。
底座是 openJiuwen 的 **DeepAgent harness**（`openjiuwen.harness.create_deep_agent`），
本项目在上面加了自己的人设、工具、安全 rails 和终端交互。

## 快速开始

需要 **Python 3.11 – 3.13**（openjiuwen 的硬性要求）。macOS 自带的 `python3` 是 3.9，
用它建的虚拟环境里 `pip3 install openjiuwen` 会报 `No matching distribution found`。

```bash
cd kernel

# 方式一：uv（推荐，会自动下载 Python 3.11，不需要管理员权限）
curl -LsSf https://astral.sh/uv/install.sh | sh        # 已装过 uv 可跳过
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -U openjiuwen && uv pip install -e ".[dev]"

# 方式二：Homebrew
brew install python@3.13
python3.13 -m venv --clear .venv && source .venv/bin/activate
pip3 install -U openjiuwen && pip3 install -e ".[dev]"

python --version            # 确认是 3.11+
pytest -q                   # 用装好的 SDK 跑离线测试 + 假模型端到端测试

cp models.example.toml models.toml   # 供应商、地址、常用模型
cp .env.example .env                 # 填你要用的供应商的 API key
mole --list-models          # 看看哪些模型可用
mole --check                # ping 一次当前模型，确认配置可用
cd ~/code/your-project && mole
```

常用参数：`-m 供应商/模型`（指定模型）、`-p "解释一下这个仓库"`（单次执行）、`--project DIR`、
`-y`（全部自动执行，慎用）、`-v`（显示思考过程）、`--list-models`。

REPL 内命令：`/model` 切换模型，`/models [供应商]` 在线查询可用模型，`/review [补充要求]` 检视未提交改动，
`/new` 新会话，`/usage` token 用量，`/help`，`/exit`；`Ctrl+C` 打断当前任务，`Ctrl+D` 退出。

## 选择供应商和模型

供应商在 `models.toml` 里配置（查找顺序：`~/.mole-agent/models.toml` → 本项目根目录 `models.toml`），
key 放在 `.env` 里，通过 `api_key_env` 引用：

```toml
default = "deepseek/deepseek-flash"

[providers.deepseek]
client_provider = "DeepSeek"                 # openjiuwen 的协议/厂商标识
api_base = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-flash", "deepseek-v4-pro"]   # 常用模型，/model 里按序号选
```

`models.example.toml` 已预置 DeepSeek、通义（DashScope）、Kimi、智谱、华为云 MaaS、硅基流动、
OpenRouter、OpenAI、Anthropic，以及公司内部 OpenAI 兼容网关的写法（自定义请求头、自签证书）。
供应商级还可以单独设 `temperature` / `max_tokens` / `timeout` / `verify_ssl` / `headers`。

三种写法指定模型，`-m` 和 `/model` 通用：

| 写法 | 含义 |
|---|---|
| `kimi/kimi-k3` | 供应商/模型；模型不必在 `models` 列表里，模型名本身带 `/` 也行（如 `openrouter/anthropic/claude-x`） |
| `kimi` | 该供应商列表里的第一个模型 |
| `kimi-k3` | 按模型名在各供应商的列表里查找，唯一命中即可 |

- 启动时用哪个：`-m` 指定的 > 上次 `/model` 切换的（记在 `~/.mole-agent/state.json`）> `default`。
- REPL 里 `/model` 切换**不重建 agent**，对话上下文、工具、「总是允许」记录都保留；
  上下文压缩仍使用启动时的模型。
- 没配 key 的供应商不会出现在 `/model` 的编号里，列表末尾会提示要设置哪个环境变量。
- `.env` 里旧的单模型写法（`MOLE_PROVIDER` / `MOLE_API_BASE` / `MOLE_API_KEY` / `MOLE_MODEL`）仍然有效，
  会以 `default` 供应商出现在列表里；没有 `models.toml` 时它就是默认模型。

## 架构

```
┌─────────────────────────── mole_agent（本项目）──────────────────────────┐
│ cli.py      REPL / 流式渲染 / 人工确认与反问 / 斜杠命令 / Ctrl+C 中断      │
│ agent.py    组装：模型 + 提示词 + 工具 + rails + MCP → create_deep_agent   │
│ prompts.py  人设 + 工作准则 + 运行环境 + 项目记忆（MOLE.md/AGENTS.md…）    │
│ tools/      自定义工具，每个工具一个文件，启动时自动发现                  │
│ rails.py    CommandGuardRail / ApprovalRail / ToolTraceRail / TokenUsage │
│ models.py   models.toml → 供应商/模型目录、选择规则、在线查询 /models      │
│ config.py   MOLE_* 环境变量 + 选中的模型 → Settings                       │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
┌──────────── openjiuwen.harness（DeepAgent）───────────────────────────────┐
│ 任务循环 · todo 规划 · 上下文压缩 · 技能 · 子智能体 · 工作区 · 安全提示     │
│ SysOperationRail：read_file / write_file / edit_file / glob / grep / bash │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
┌──────────── openjiuwen.core ──────────────────────────────────────────────┐
│ Model（OpenAI 兼容 / Anthropic）· Tool · Session · Runner · ReAct 循环     │
└───────────────────────────────────────────────────────────────────────────┘
```

一次工具调用经过的 rails（`before_tool_call`，priority 高者先执行）：

```
模型发起 tool_call
  → CommandGuardRail (95)  bash 命中拒绝规则 → 直接驳回，模型收到原因
  → ApprovalRail     (90)  write_file / edit_file / bash → 中断，等你 y / a / n
  → ToolTraceRail    (5)   输出 ● tool(args) 到终端
  → 执行工具（文件工具受沙箱限制：只能访问项目目录、~/.mole-agent/workspace 和技能目录）
  → ToolTraceRail          输出 ⎿ 结果摘要，写审计日志 ~/.mole-agent/audit.jsonl
```

## 安全模型

| 层 | 机制 | 配置 |
|---|---|---|
| 文件沙箱 | 文件工具和命令里引用的路径只能落在项目目录、agent 工作区和技能目录 | `MOLE_RESTRICT_TO_PROJECT` |
| 命令黑名单 | `CommandGuardRail`：sudo、rm -rf /、强推、reset --hard、curl\|sh 等直接驳回 | `config.DEFAULT_BASH_DENY` + `MOLE_EXTRA_BASH_DENY` |
| 人工确认 | `ApprovalRail`：写文件、改文件、执行命令前询问；`a` = 本会话总是允许 | `MOLE_CONFIRM_TOOLS`，`-y` 关闭 |
| 审计 | 每次工具调用一行 JSONL | `MOLE_AUDIT_LOG` |

> 注意：SDK 的 `BashTool` 自带 `deny_patterns`，但只在环境变量 `OPENJIUWEN_BASH_STRICT=1` 时生效，
> 所以本项目用 `CommandGuardRail` 在 rail 层统一兜底。

## 怎么扩展

**加一个工具**：每个工具是 `mole_agent/tools/` 下的一个文件，启动时自动发现，不需要改其他代码。

```
mole_agent/tools/
├── __init__.py          自动发现与注册（build_custom_tools）
├── _common.py           共用函数：resolve_inside（路径限制在项目内）、truncate（输出截断）
├── _template.py         新工具模板（下划线开头的文件不会被加载）
├── git_changes.py
└── project_overview.py
```

复制 `_template.py` 为 `<工具名>.py`（文件名与工具名一致），实现 `create(settings) -> Tool`，重启 mole 即可。
需要按配置开关时再加一个 `enabled(settings) -> bool`。`description` 要写清「什么时候该用」，模型靠它做选择；
模板开头有完整的检查清单。模块写错（缺 `create`、工具名重复、`create` 报错）时启动会直接报出是哪个文件。

**加一个 rail**（`rails.py`）：继承 `AgentRail`，覆写需要的钩子（`before_model_call` / `after_tool_call` 等），
设好 `priority`，在 `agent.py` 的 `rails` 列表里挂上。要「拦截并裁决」工具调用就继承 `BaseInterruptRail`，
在 `resolve_interrupt` 里返回 `approve()` / `reject(...)` / `interrupt(...)`。

**接 MCP 服务**：在项目根目录放 `.mcp.json`（或 `~/.mole-agent/mcp.json`），格式与 Claude Code 相同：

```json
{"mcpServers": {
  "fs":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
  "remote": {"type": "http", "url": "http://mcp.internal/mcp", "headers": {"Authorization": "Bearer xxx"}}
}}
```

**加技能**：`SKILL.md` 放到 `<项目>/.mole/skills/<名字>/`（只在该项目生效）或
`~/.mole-agent/skills/<名字>/`（所有项目共用），格式与 Claude Code skills 一致，也可以用软链接复用
`~/.claude/skills` 里的技能。技能名取目录名；新增、修改技能不用重启（会话中途新增的技能 `/new` 后进入技能列表）。
技能目录（包括软链接指向的目录）会自动加入文件沙箱，技能里的 `references/`、`scripts/` 都能被读取；
启动后才新建的软链接需要重启 mole 才会加入沙箱。示例见 `examples/skills/commit-message/`。

本仓库自带一个项目级技能 `.mole/skills/code-review/`：在 kernel 目录下运行 mole 时，说「检视一下我的改动」
或执行 `/review` 就会按它做代码检视（`SKILL.md` 定流程和输出格式，`references/` 里是本项目约定和细项清单）。

**项目约定**：在项目根目录写 `MOLE.md`（也兼容 `AGENTS.md` / `CLAUDE.md`），启动时注入系统提示词。

**子智能体**：`create_deep_agent(subagents=[SubAgentConfig(...)])`，可参考
`openjiuwen/harness/cli/agent/factory.py` 里的 `_build_subagents`。

## 文件位置

| 路径 | 内容 |
|---|---|
| `~/.mole-agent/.env` | 可选的全局配置（优先级低于本项目根目录的 `.env` 和环境变量） |
| `~/.mole-agent/models.toml` | 可选的全局供应商配置（优先于本项目根目录的 `models.toml`） |
| `~/.mole-agent/state.json` | 上次 `/model` 切换到的模型 |
| `~/.mole-agent/workspace/` | agent 私有工作区（记忆、todo 等），与项目目录隔离 |
| `~/.mole-agent/logs/` | SDK 日志（不输出到终端，也不写进项目目录） |
| `~/.mole-agent/audit.jsonl` | 工具调用审计 |
| `~/.mole-agent/history` | REPL 输入历史 |

## 测试

```bash
pytest -q        # 不联网、不需要 API key
```

- `tests/test_offline.py`：自定义工具的行为、命令拒绝规则、提示词、配置、MCP 解析、agent 组装
- `tests/test_tools.py`：tools/ 目录约定——自动发现、文件名即工具名、模板可用、写错时的报错
- `tests/test_models.py`：models.toml 解析、模型选择规则、启动优先级、供应商级参数覆盖
- `tests/test_skills.py`：仓库自带技能的格式检查；项目级 / 用户级 / 软链接技能能被加载和读取，沙箱外仍被拦截
- `tests/test_e2e_fake_llm.py`：用 pip 装好的 openjiuwen SDK + 按剧本回复的假模型，
  跑完整流程（工具调用、确认/拒绝/总是允许、危险命令拦截、文件沙箱、审计日志、多轮上下文、运行中切换模型）

真实模型用 `mole --check` 验证连通性。

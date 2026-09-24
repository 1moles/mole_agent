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
`-c`（继续当前项目最近的会话）、`-r [序号]`（选一个历史会话继续）、
`-y`（全部自动执行，慎用）、`-v`（显示思考过程）、`--list-models`。

### Windows

在 PowerShell 里执行（推荐用 Windows Terminal）：

```powershell
# 1. Python 3.13 和 Git。Git for Windows 自带 Git Bash，agent 执行 ls、grep 这类命令时会用到
winget install -e --id Python.Python.3.13
winget install -e --id Git.Git
# 装完重新打开终端

# 2. 取代码、建虚拟环境（py 启动器能准确选中 3.13，避开 Microsoft Store 的 python 占位程序）
git clone https://github.com/1moles/mole_agent.git kernel
cd kernel
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1      # 报「禁止运行脚本」时先执行：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -U pip
pip install -U openjiuwen
pip install -e ".[dev]"

# 3. 配置（models.toml 已在仓库里）
Copy-Item .env.example .env
notepad .env

# 4. 运行
$env:PYTHONUTF8 = "1"; setx PYTHONUTF8 1   # 建议：Python 统一按 UTF-8 读写（当前和以后的终端）
pytest -q
mole --check
cd D:\code\your-project; mole
```

用 uv 也可以：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` 装好 uv 后，
`uv venv --python 3.13 .venv`，激活，再 `uv pip install -U openjiuwen` 和 `uv pip install -e ".[dev]"`。

和 macOS / Linux 的差异：

- 配置目录 `~/.mole-agent/` 在 Windows 上是 `C:\Users\<用户名>\.mole-agent\`。
- 执行命令：openjiuwen 的 `bash` 工具在 Windows 上按命令自动选 shell——PowerShell 语法交给 PowerShell，
  `ls` / `grep` 这类交给 Git Bash，其余交给 cmd；另外还会多注册一个 `powershell` 工具。
  两者都要你确认（`MOLE_CONFIRM_TOOLS` 里 `bash` 和 `powershell` 算一组），也都受拦截规则约束
  （包括 `Remove-Item` 删整个盘、`Format-Volume`、`iwr … | iex` 等）；子 agent 不能用 `powershell`。
- `Ctrl+C` 只打断当前任务，不会退出程序。
- 技能软链接需要打开「开发者模式」（设置 → 系统 → 开发者选项）才能创建，否则直接复制技能目录；
  测试里的软链接用例在不能建软链接时自动跳过。
- 老式 cmd 窗口里中文和颜色可能显示不正常，用 Windows Terminal 即可。

### 斜杠命令与补全

REPL 内命令：`/model` 切换模型，`/models [供应商]` 在线查询可用模型，`/review [范围或要求]` 交给检视子 agent
（默认未提交改动，也可以 `/review 提交 a1b2c3d`、`/review 和 main 比`），
`/new` 新会话，`/history [序号|关键词]` 查看 / 搜索历史会话，`/resume [序号|关键词]` 回到历史会话继续聊，`/usage` token 用量，
`/context` 上下文占用，`/compact [要保留的重点]` 手动压缩上下文，`/help`，`/exit`；
`Ctrl+C` 打断当前任务，`Ctrl+D` 退出。

输入 `/` 自动展开命令菜单，继续输入可过滤候选；↑↓ 选择、Tab 补全，选中候选后 Enter 确认，再次 Enter 提交。Esc 关闭菜单并保留输入。`/model ` 和 `/models ` 支持本地配置候选补全。

`/history` 和 `/resume` 同样支持命令名补全；会话序号或 id 需手动输入，暂不提供会话参数候选。

### 向你提问（question 工具）

需求不明确、有几种做法可选、或者执行中发现情况和预期不一样时，agent 会调用常驻的 `question` 工具停下来问你
（代替 SDK 自带的 `ask_user`）。一次可以问几个问题，每个问题有单选或多选，选项最后自动加上「自己输入答案」：

```
? 需要你的输入（共 2 个问题）

[数据库] 用哪个数据库？ （单选）
  1. PostgreSQL (Recommended) - 功能全
  2. SQLite - 零配置
  3. 自己输入答案
输入序号，或直接输入答案；回车跳过 › 1

[功能] 要哪些功能？ （可多选）
  1. 登录
  2. 日志
  3. 缓存
输入序号，多个用逗号或空格隔开；回车跳过 › 1,3
● question(数据库、功能)
  ⎿ "用哪个数据库？" = ["PostgreSQL (Recommended)"]
  ⎿ "要哪些功能？" = ["登录", "缓存"]
```

- 输入序号（多选用逗号、空格或顿号隔开），也可以直接打选项的文字；允许自定义时直接打你自己的答案，
  或者选「自己输入答案」那一项再输入。回车跳过这个问题，agent 会自己拿主意。
- 回答以标签数组交回 agent；回答中途 `Ctrl+C` 会中断任务，续聊（`/resume`、`mole -c`）时先把问题重新问一遍。
- 常驻：工具卡片声明为直接可见，即使打开 SDK 的渐进式工具加载（`tool_search`）也不会被藏起来。
- 子 agent 没有这个工具（它们在 `task_tool` 里运行，问题传不到终端），会按最合理的理解继续并写明假设。

### 会话历史与续聊

每次对话都按 openjiuwen 自带的 `SessionStore`（`openjiuwen.harness.cli.storage`）格式记下来，一个会话一个 JSON 文件，
按项目分目录存在 `~/.mole-agent/sessions/<项目名>-<哈希>/`。`/history` 列出当前项目的会话（最近活动的在前，
标题是第一条输入），输入序号看详情；也可以直接 `/history 2` 或 `/history mole-1a2b`（会话 id 前缀）。

**搜索**：`/history 关键词` 在标题和全部内容（你的输入、回复、工具调用摘要）里找，不区分大小写，不要求开头匹配；
多个关键词用空格隔开，要都出现才算命中；纯数字会被当成序号，要搜数字或带空格的短语就加引号：`/history "8080"`。
结果里会标出命中的那一段，选序号看详情。`/resume 关键词` 同样可以先搜再选。

```
› /history 你好
搜索「你好」 找到 2 个会话 · 最近活动的在前
   1. 09-24 10:45   1 轮 · deepseek/deepseek-flash  修一下登录的 bug
      回复：…好了，登录失败时会提示原因。顺便跟你说声你好。
   2. 09-24 10:40   1 轮 · deepseek/deepseek-flash  你好
      你：你好
输入序号查看详情（回车返回） ›
```

```
历史会话 kernel · 最近活动的在前
   1. 09-24 09:22   1 轮 · deepseek/deepseek-flash  /review  ← 当前
   2. 09-24 09:15   3 轮 · deepseek/deepseek-flash  帮我看看 tools 目录下有哪些工具
输入序号查看详情（回车返回） › 2
──────────── mole-1a2b3c4d · 09-24 09:15 · deepseek/deepseek-flash · 3 轮 ────────────
› 09:15 帮我看看 tools 目录下有哪些工具
  ⎿ glob(mole_agent/tools/*.py) → 成功：mole_agent/tools/git_changes.py
● tools 目录下有两个工具：git_changes（取代码改动）和 project_overview（仓库概览）。
```

- 记录的内容：你的输入（斜杠命令记原样，如 `/review`）、回复文本、每次工具调用的一行摘要（成功 / 失败 / 被拦截 / 用户拒绝），
  以及切换模型、中断、出错。子 agent 内部的工具调用不记，只记它交回的结果。
- `MOLE_SAVE_HISTORY=false` 关闭记录（续聊也一起关掉）。历史和检查点里都有对话原文，别把 `~/.mole-agent/` 发给别人。

**继续聊**：看完详情按 `r`，或者 `/resume [序号|关键词]`，就回到那个会话，agent 记得之前的全部上下文
（包括工具调用和结果，不只是历史里的摘要）。启动时 `mole -c` 直接继续当前项目最近的会话，`mole -r` 先列出来选。

```
› /resume 2
✓ 已回到会话 mole-1a2b3c4d · 3 轮 · 对话上下文已恢复
  上次问：帮我看看 tools 目录下有哪些工具
  上次答：tools 目录下有两个工具：git_changes（取代码改动）和 project_overview（仓库概览）。
```

- 原理：openjiuwen 默认把 agent 状态（上下文、待确认的操作）放在内存里，重启就没了。Mole 换成 SDK 自带的
  `PersistenceCheckpointer`，每轮结束存进 SQLite（`~/.mole-agent/checkpoints.db`）；用原来的会话 id 再跑，SDK 自动恢复。
  需要 `aiosqlite`（已写进依赖，升级后重新执行一次 `pip install -e ".[dev]"`）；缺了会提示，其他功能照常。
- 用的是当前选中的模型，不会切回原会话的模型；「总是允许」不跟着历史会话走，回到旧会话后会重新询问。
- 上次如果停在等你确认的操作上（比如在确认提示处按了 Ctrl+C），继续后会先重新问你那一步。
- 开启续聊之前的会话只有历史记录、没有检查点，只能查看，不能继续。

### 上下文压缩

长对话由 SDK 自动压缩（`ContextProcessorRail` 的预设处理器链，`MOLE_ENABLE_CONTEXT_RAILS=false` 可关闭）：

```
每次调模型前
  ├─ 单条工具输出超过窗口 10%  → 转存全文，上下文里只留开头和结尾的预览（不调模型）
  ├─ 占用达到窗口 80%          → 调模型把较早的对话压成摘要，最近的消息原样保留
  └─ 模型报「上下文超长」       → 主动压缩一次，再重试这一步（只重试一次）
```

压缩时终端提示一行（压缩要等一次模型调用，超过 1 秒会先显示「正在压缩…」，完成后接在同一行后面）：

```
  ⟳ 上下文已压缩：对话约 152.3k → 18.6k tokens（省 88%）
  ⟳ 模型报上下文超长，已压缩上下文：对话约 30.1k → 6.2k tokens（省 79%），重试中
  ⚠ 上下文压缩失败，上下文保持原样：HTTPStatusError: Client error '400 Bad Request' ...
```

`/context` 看当前占用、窗口大小和它的来源、自动压缩的阈值，以及最近一次请求里系统提示、工具定义、对话各占多少：

```
› /context
上下文  约 38.2k / 200k tokens（19%）
  ████████░░░░░░░░░░░░░░░░░░░░░░░░│░░░░░░░  到 80%（约 160k）自动压缩
  窗口  200k（默认值：inner-model 不在 SDK 内置的模型表里）
  对话  8 轮 · 42 条消息（用户 8 · 助手 17 · 工具结果 17）
  最近一次请求  系统提示 6.1k · 工具定义 9.8k · 技能 1.2k · 对话 21.1k
```

`/compact` 不等到 80% 就先压缩，后面可以写上要特别保留的内容，例如 `/compact 保留登录模块的改动和没做完的待办`。
压缩结果写回检查点，重启后 `/resume` 回来也是压缩后的上下文。执行中 `Ctrl+C` 可以取消，上下文不变。

- 压缩用的是**启动时**的模型（`/model` 切换后不变），`stream_only` 也按启动时的供应商。
- 窗口大小按模型名查 SDK 内置的表，查不到按 200k 算——内部模型基本都查不到，`/context` 会显示「默认值」。
  实际窗口更小的话，要等接口报超长才会压缩。
- 压缩失败（比如压缩请求被网关拒绝）时 SDK 只记日志、上下文不变，Mole 会把原因打出来。
- 「上下文超长」的判断：SDK 认英文报错（`maximum context length`、`prompt is too long` 等），Mole 补上了中文网关
  常见的写法（「输入长度超过…上下文」「上下文超长」之类）。
- 续聊回来还没发消息时不能 `/compact`：rails（包括压缩器）要到第一次对话时才装上，先聊一句即可。

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

**不用 API key、靠请求头鉴权**（如 ModelArts 在线服务的 `X-Auth-Token` / `X-Apig-AppCode`）：写 `auth = "headers"`，
请求头的值用 `${环境变量}` 引用，真正的 token 放 `.env`：

```toml
[providers.modelarts]
client_provider = "ModelArts"
api_base = "https://<在线服务调用地址>/v1"
auth = "headers"                                      # 不发 Authorization，只发下面的请求头
headers = { "X-Auth-Token" = "${MODELARTS_TOKEN}" }   # .env 里写 MODELARTS_TOKEN=...
models = ["<部署的模型名>"]
```

| `auth` | 发送的鉴权信息 |
|---|---|
| `api_key`（默认） | `Authorization: Bearer <key>`，`headers` 作为附加请求头 |
| `headers` | 只发 `headers`；只配了 `headers`、没配任何 key 时自动按这种方式处理 |
| `none` | 不鉴权（本地 vLLM、不校验的内部网关） |

- 变量没设置时，该供应商不会出现在 `/model` 的编号里，列表末尾会提示缺哪个变量。
- `Authorization` 不能写进 `headers`（openjiuwen 会丢弃它），Bearer 鉴权请用 `api_key_env`。
- `mole --check` 会显示用的是哪种鉴权、带了哪些请求头（只显示名字，不显示取值）。
- `/models` 在线查询用同样的请求头。

**只接受流式请求的模型**（要求请求里 `stream=true`）：在供应商下加 `stream_only = true`。agent 对话本来就是流式的，
打开后 SDK 里其余的非流式调用（任务完成判断、上下文压缩、`mole --check` 等）也改为流式请求、在本地拼成完整回复。
上下文压缩器是 SDK 按启动时的配置自己建的模型客户端，不经过 Mole 的模型对象；`rails.CompressionRail` 把压缩器的
客户端配置换成 SDK 客户端注册表里登记的只走流式的客户端（`agent.stream_only_client_config`）。
不要自己往 `ModelRequestConfig` 里加 `stream=True`：它会被原样塞进非流式调用的请求参数，
报 `'AsyncStream' object has no attribute 'choices'`。

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
- 子 agent 默认跟随主 agent 当前模型；想单独指定（比如检视用更强的模型）就在 `models.toml` 里加
  `[subagents]` 表，例如 `code_reviewer = "deepseek/deepseek-v4-pro"`，见 `models.example.toml`。
- `.env` 里旧的单模型写法（`MOLE_PROVIDER` / `MOLE_API_BASE` / `MOLE_API_KEY` / `MOLE_MODEL`）仍然有效，
  会以 `default` 供应商出现在列表里；没有 `models.toml` 时它就是默认模型。

## 架构

```
┌─────────────────────────── mole_agent（本项目）──────────────────────────┐
│ cli.py      REPL / 流式渲染 / 人工确认与反问 / 斜杠命令 / Ctrl+C 中断      │
│ commands/   命令注册 / 分发 / 菜单补全 / 帮助生成                         │
│ agent.py    组装：模型 + 提示词 + 工具 + rails + MCP → create_deep_agent   │
│ prompts.py  人设 + 工作准则 + 运行环境 + 项目记忆（MOLE.md/AGENTS.md…）    │
│ tools/      自定义工具，每个工具一个文件，启动时自动发现                  │
│ subagents/  子 agent，每个一个文件：explore_agent、code_reviewer          │
│ rails.py    CommandGuard / Approval / ReadOnlyShell / ToolTrace / Usage   │
│ context.py  /context、/compact、压缩提示、压缩器只走流式、压缩失败原因    │
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
  → ApprovalRail     (90)  write_file / edit_file / bash / powershell → 中断，等你 y / a / n
  → ToolTraceRail    (5)   输出 ● tool(args) 到终端
  → 执行工具（文件工具受沙箱限制：只能访问项目目录、~/.mole-agent/workspace 和技能目录）
  → ToolTraceRail          输出 ⎿ 结果摘要，写审计日志 ~/.mole-agent/audit.jsonl
```

主 agent 通过 `task_tool` 把任务派给子 agent。子 agent 在独立上下文里跑完，只把结论交回主 agent：

```
主 agent ── task_tool(subagent_type="code_reviewer", task_description="检视未提交改动")
               │  SDK 现场创建子 agent（上下文只有任务描述），和主 agent 共用沙箱和 token 统计
               ▼
            code_reviewer：skill_tool 读 code-review 技能 → git_changes → read_file / grep …
               │  bash 经 ReadOnlyShellRail：只读命令放行，其余直接驳回（不弹确认）
               │  每个工具调用 → 审计（agent=code_reviewer）+ 终端里以 │ 开头的一行
               ▼
主 agent ◀── 检视报告（主 agent 转述给你；要改的地方由主 agent 来改，照常需要你确认）
```

## 安全模型

| 层 | 机制 | 配置 |
|---|---|---|
| 文件沙箱 | 文件工具和命令里引用的路径只能落在项目目录、agent 工作区和技能目录 | `MOLE_RESTRICT_TO_PROJECT` |
| 命令黑名单 | `CommandGuardRail`：sudo、rm -rf /、强推、reset --hard、curl\|sh，以及 Windows 上删整个盘、格式化、iwr\|iex 等直接驳回；命令按 `\|` `&&` `;` 单个 `&` 和换行切开逐段检查 | `config.DEFAULT_BASH_DENY` + `MOLE_EXTRA_BASH_DENY` |
| 人工确认 | `ApprovalRail`：写文件、改文件、执行命令（bash / powershell）前询问；`a` = 本会话总是允许 | `MOLE_CONFIRM_TOOLS`，`-y` 关闭 |
| 子 agent 只读 | 没有写文件工具和 `question`；bash 只放行 `ls` / `cat` / `grep` / `git diff` 等只读命令，powershell 一律拒绝（`ReadOnlyShellRail`）；不挂人工确认 | `rails.read_only_violation` |
| 审计 | 每次工具调用一行 JSONL（`agent` 字段区分主 agent 和各子 agent） | `MOLE_AUDIT_LOG` |

> 注意：SDK 的 `BashTool` 自带 `deny_patterns`，但只在环境变量 `OPENJIUWEN_BASH_STRICT=1` 时生效，
> 所以本项目用 `CommandGuardRail` 在 rail 层统一兜底。
>
> 子 agent 为什么只读：`task_tool` 在内部运行子 agent，子 agent 触发的人工确认不会传到终端
>（openjiuwen 0.1.18），给它写权限就只能要么卡住、要么绕过确认。

## 怎么扩展

**加一个斜杠命令**：在 `mole_agent/commands/__init__.py` 的 `default_registry()` 里注册 `CommandSpec`，
填写名称、说明和异步处理函数；按需提供用法、别名、示例、参数补全或可用性判断。菜单、帮助和分发自动读取同一份定义。

处理函数通过 `CommandContext` 访问运行时能力；沿用现有内置命令的 `ctx.actions[name]` 适配方式时，
还需在 `cli.py` 的 `Repl.command_context()` 中接入对应操作。无需修改通用分发器或单独维护帮助命令列表。
架构、取舍与当前限制见 [斜杠命令设计](docs/slash-command-architecture.md)。

**加一个工具**：每个工具是 `mole_agent/tools/` 下的一个文件，启动时自动发现，不需要改其他代码。

```
mole_agent/tools/
├── __init__.py          自动发现与注册（build_custom_tools）
├── _common.py           共用函数：resolve_inside（路径限制在项目内）、truncate（输出截断）
├── _template.py         新工具模板（下划线开头的文件不会被加载）
├── git_changes.py
├── project_overview.py
└── question.py          向用户提问；由 rails.QuestionRail 中断等回答，不会真正执行
```

复制 `_template.py` 为 `<工具名>.py`（文件名与工具名一致），实现 `create(settings) -> Tool`，重启 mole 即可。
需要按配置开关时再加一个 `enabled(settings) -> bool`；工具不改任何东西时加 `READ_ONLY = True`，
子 agent 才会拿到它（不写就只给主 agent）。`description` 要写清「什么时候该用」，模型靠它做选择；
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

**加一个子 agent**：和工具一样，一个子 agent 一个文件，启动时自动发现。

```
mole_agent/subagents/
├── __init__.py          自动发现与注册（build_subagents）
├── _common.py           SubagentEnv：只读 rails、只读工具、技能、沙箱、按 [subagents] 选模型
├── explore_agent.py     只读探索：直接用 SDK 的 build_explore_agent_config（提示词/描述都是 SDK 自带的）
└── code_reviewer.py     代码检视：有 code-review 技能就按技能执行，返回分级的检视报告
```

新建 `<子agent名>.py`，实现 `create(env: SubagentEnv) -> SubAgentConfig`（可选 `enabled(settings)`），
rails 用 `env.read_only_rails(名字)`，工具用 `env.tools()`，模型用 `env.model_for(名字)`，
再传上 `workspace=env.workspace(名字)`、`sys_operation=env.sys_operation`，参考 `code_reviewer.py`。
`agent_card.description` 要写清「什么时候派给它、task_description 写什么」，主 agent 靠它决定何时派活。
`MOLE_ENABLE_SUBAGENTS=false` 可以整体关掉。

## 连不上模型怎么查

`mole --check` 调用失败时会打出异常链（最后一行是根因），连接类错误还会按 openjiuwen 的实际行为逐步检查：
走不走代理、NO_PROXY 是否生效、DNS、TCP、代理 CONNECT、TLS 握手。把这段输出发给维护者即可。常见原因：

| 现象（诊断里的 ✗） | 原因与处理 |
|---|---|
| 会走代理 / 连接代理失败 / 代理 CONNECT 403、502 | 终端里设了 `http_proxy` 等代理变量，内网地址也被发给了代理。把域名加进 `NO_PROXY`，**要带前导点**：`.inner.example.com` |
| NO_PROXY 写法 | openjiuwen 的 NO_PROXY 规则比 curl 严：`example.com` 不包含子域名，`*.example.com` 不支持，要写成 `.example.com`；它还会优先用 `http_proxy`（即使地址是 https）|
| DNS 解析失败 / TCP 连接超时 | 不在内网、没连 VPN，或所在网络区域到不了这个地址 |
| 证书校验失败 | 服务用公司内部 CA 的证书而本机 Python 不信任它；python.org 安装包装的 Python 要先运行「Install Certificates.command」。可把内部根证书加入信任库，或在该供应商下临时写 `verify_ssl = false` |
| TLS 协商失败 | openjiuwen 只允许 TLS1.2+，TLS1.2 下只允许 ECDHE + AES-GCM 套件；诊断会对比 Python 默认配置能否握手 |

另外先看 `--check` 前几行：「模型」和「配置」说明实际用的是哪个供应商、哪份 `models.toml`。
`~/.mole-agent/models.toml` 优先于仓库里的，`~/.mole-agent/state.json` 会记住上次 `/model` 选的模型。

## 文件位置

| 路径 | 内容 |
|---|---|
| `~/.mole-agent/.env` | 可选的全局配置（优先级低于本项目根目录的 `.env` 和环境变量） |
| `~/.mole-agent/models.toml` | 可选的全局供应商配置（优先于本项目根目录的 `models.toml`） |
| `~/.mole-agent/state.json` | 上次 `/model` 切换到的模型 |
| `~/.mole-agent/workspace/` | agent 私有工作区（记忆、todo 等），与项目目录隔离；子 agent 的在 `sub_agents/<名字>/` |
| `~/.mole-agent/logs/` | SDK 日志（不输出到终端，也不写进项目目录） |
| `~/.mole-agent/audit.jsonl` | 工具调用审计 |
| `~/.mole-agent/history` | REPL 输入历史（上下键翻的那个） |
| `~/.mole-agent/sessions/` | 会话历史（`/history`），每个项目一个子目录，每个会话一个 JSON |
| `~/.mole-agent/checkpoints.db` | 续聊用的对话上下文（SDK 的 SQLite 检查点），`/resume`、`mole -c` 从这里恢复 |

## 测试

```bash
pytest -q        # 不联网、不需要 API key
```

- `tests/test_offline.py`：自定义工具的行为、命令拒绝规则、提示词、配置、MCP 解析、agent 组装
- `tests/test_tools.py`：tools/ 目录约定——自动发现、文件名即工具名、模板可用、写错时的报错
- `tests/test_commands.py`：命令注册与别名冲突、分发与错误处理、异步补全、输入框 Enter / Esc 行为、
  帮助内容与样式，以及新增命令和示例的自动接入
- `tests/test_models.py`：models.toml 解析、模型选择规则、启动优先级、供应商级参数覆盖
- `tests/test_history.py`：会话历史——SDK 的文件格式、按项目分目录、列表排序和标题、损坏文件跳过、
  `/history` 列表 / 序号 / id 前缀看详情、关键词模糊搜索（标题和内容、不区分大小写、多词、引号），斜杠命令、切换模型、出错、中断都会记下（假模型端到端）
- `tests/test_resume.py`：续聊——两次「重启」之间恢复完整上下文、`mole -c` 选最近的会话、新会话不带旧上下文、
  待确认的操作续聊后重新询问、没有检查点的旧会话被拒绝、「总是允许」不跟随、缺 aiosqlite 时降级
- `tests/test_netcheck.py`：连接诊断——异常链、openjiuwen 与 httpx 的代理选择差异、NO_PROXY 写法、
  DNS / TCP / 代理 CONNECT / 证书 / 加密套件各步骤（本机临时服务，不联网）
- `tests/test_stream_only.py`：起一个拒绝非流式请求的假网关，确认 `stream_only` 下 `--check`、带解析器的调用、
  工具调用、上下文压缩都能通过流式拼接得到完整结果；没打开时压缩被拒，终端说明原因
- `tests/test_question.py`：`question` 工具——常驻声明、参数校验、单选 / 多选 / 自定义 / 跳过的输入解析、
  问答后继续执行、输错重问、参数不对时不打扰用户直接让模型重来、中断后续聊重新提问（假模型端到端）
- `tests/test_context.py`：`/context` 的占用和阈值、`/compact`（带保留要求、失败原因、重启后仍是压缩后的上下文、
  续聊后未发消息时不装压缩器也不会弄丢压缩器）、自动压缩的一行提示（慢时先显示进度）、
  模型报超长（英文 / 中文报错）后压缩重试
- `tests/test_windows.py`：powershell 同样受确认 / 拦截 / 只读守卫约束，Windows 危险命令用例表，
  `&` 和换行不能绕过命令检查，事件循环不支持信号处理时 Ctrl+C 只打断当前任务
- `tests/test_header_auth.py`：请求头鉴权的解析与校验；起一个本地假服务，确认对话、流式、`--check`、`/models`
  发出的请求都带上了配置的请求头且没有 `Authorization`
- `tests/test_skills.py`：仓库自带技能的格式检查；项目级 / 用户级 / 软链接技能能被加载和读取，沙箱外仍被拦截
- `tests/test_subagents.py`：只读 shell 放行/拦截用例表、`git_changes` 的提交/分支范围、子 agent 自动发现与只读配置、
  `[subagents]` 选模型，以及主 agent → `task_tool` → `code_reviewer` 的端到端流程（写命令被拒、不弹确认、审计带 agent 名）
- `tests/test_e2e_fake_llm.py`：用 pip 装好的 openjiuwen SDK + 按剧本回复的假模型，
  跑完整流程（工具调用、确认/拒绝/总是允许、危险命令拦截、文件沙箱、审计日志、多轮上下文、运行中切换模型）

真实模型用 `mole --check` 验证连通性。

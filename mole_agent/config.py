"""运行配置：从环境变量 / .env 读取，统一 MOLE_ 前缀；模型供应商见 models.py / models.toml。

加载顺序（先加载的优先，已存在的环境变量不会被覆盖）：
    进程环境变量 > <本项目根目录>/.env > ~/.mole-agent/.env

启动时使用哪个模型：命令行 -m > 上次用 /model 切换的模型 > models.toml 里的 default
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from mole_agent.models import (
    ModelCatalog,
    ModelChoice,
    ModelSelectionError,
    load_catalog,
    load_last_model,
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _home_dir() -> Path:
    return Path(os.getenv("MOLE_HOME", Path.home() / ".mole-agent")).expanduser()


# 执行命令的工具：bash（Windows 上 SDK 会按命令自动选 PowerShell / Git Bash / cmd），
# 以及只在 Windows 上注册的 powershell。确认、拦截规则对两者一视同仁。
SHELL_TOOLS = ("bash", "powershell")


def with_shell_group(tools: list[str]) -> list[str]:
    """确认列表里写了 bash 或 powershell 之一，就两个都确认（旧 .env 里只写了 bash 也不会漏掉 powershell）。"""
    out = list(tools)
    if any(t in SHELL_TOOLS for t in out):
        out += [t for t in SHELL_TOOLS if t not in out]
    return out


# 内置命令拒绝规则（正则，忽略大小写），由 rails.CommandGuardRail 对 bash / powershell 执行。
# 命令会先按 | && || ; 以及单个 &、换行切成子命令再逐段 search，
# 所以每条规则描述「单个子命令」长什么样即可。
DEFAULT_BASH_DENY: list[str] = [
    r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*\s+(-[a-z]*\s+)*(/\*?|~/?|\$HOME/?)\s*$",  # rm -rf / 或 ~
    r"^\s*sudo\b",                                      # 提权
    r"\bmkfs(\.\w+)?\b",                                # 格式化
    r"\bdd\b.*\bof=/dev/",                              # 覆写块设备
    r"^\s*(shutdown|reboot|halt|poweroff)\b",           # 关机重启
    r"^\s*(ba|z|da)?sh\s*(-s)?\s*$",                    # curl ... | sh 的管道右侧
    r"\bgit\s+push\b.*\s(--force|-f)(\s|$)",            # 强推
    r"\bgit\s+reset\s+--hard\b",                        # 丢弃本地修改
    r"\bchmod\s+(-R\s+)?777\s+/",                       # 全盘放权
    r":\(\)\s*\{",                                      # fork 炸弹
    # Windows（PowerShell / cmd）
    r"^\s*(stop-computer|restart-computer)\b",           # 关机重启
    r"\b(format-volume|clear-disk|initialize-disk)\b",   # 格式化 / 清盘
    r"^\s*format\s+[a-z]:",                              # cmd 格式化
    r"^\s*(iex|invoke-expression)\b",                    # iwr ... | iex 的管道右侧
    r"\b(remove-item|ri|rm|rmdir|rd|del|erase)\b.*\s['\"]?([a-z]:[\\/]?|~[\\/]?|\$home[\\/]?|\$env:userprofile[\\/]?)\*?['\"]?\s*$",  # 删整个盘 / 用户目录
]


def _get(name: str, default: str = "") -> str:
    return os.getenv(f"MOLE_{name}", default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_float(name: str, default: float | None) -> float | None:
    raw = _get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _get_list(name: str, default: list[str], sep: str = ",") -> list[str]:
    raw = os.getenv(f"MOLE_{name}")
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(sep) if item.strip()]


@dataclass
class Settings:
    # 当前使用的模型（由 models.toml 中选中的 供应商/模型 填充，/model 切换时更新）
    provider: str = "OpenAI"          # openjiuwen client_provider
    model: str = ""
    api_key: str = ""
    api_base: str = ""
    temperature: float | None = None
    max_tokens: int = 8192
    timeout: float = 120.0
    verify_ssl: bool = True
    custom_headers: dict[str, str] = field(default_factory=dict)   # 已把 ${VAR} 换成实际值
    auth: str = "api_key"             # api_key / headers / none，见 models.AUTH_MODES
    stream_only: bool = False         # 所有模型调用都走流式（网关只接受 stream=true）
    provider_name: str = ""           # models.toml 里的供应商名
    catalog: ModelCatalog = field(default_factory=ModelCatalog)
    model_error: str = ""
    # 全局默认值（供应商没单独设置时使用），在 __post_init__ 里从上面的字段捕获
    model_defaults: dict[str, Any] = field(default_factory=dict)

    # Agent
    agent_name: str = "Mole"
    language: str = "cn"
    max_iterations: int = 40

    # 路径
    project_dir: Path = field(default_factory=Path.cwd)
    home_dir: Path = field(default_factory=_home_dir)

    # 安全
    confirm_tools: list[str] = field(default_factory=lambda: ["write_file", "edit_file", "bash", "powershell"])
    restrict_to_project: bool = True
    bash_deny_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BASH_DENY))

    # 可选能力
    enable_web: bool = False
    enable_context_rails: bool = True
    enable_subagents: bool = True
    audit_log: bool = True
    verbose: bool = False

    def __post_init__(self) -> None:
        self.confirm_tools = with_shell_group(self.confirm_tools)
        if not self.model_defaults:
            self.model_defaults = {
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "timeout": self.timeout,
                "verify_ssl": self.verify_ssl,
            }

    @property
    def model_spec(self) -> str:
        return f"{self.provider_name}/{self.model}" if self.provider_name else self.model

    def apply_choice(self, choice: ModelChoice) -> None:
        """把选中的 供应商/模型 写入当前配置；供应商未设置的参数回落到全局默认值。"""
        p, d = choice.provider, self.model_defaults
        self.provider_name = p.name
        self.provider = p.client_provider
        self.model = choice.model
        self.api_base = p.api_base
        self.api_key = p.resolved_api_key if p.auth != "none" else ""
        self.custom_headers = p.resolved_headers
        self.auth = p.auth
        self.stream_only = p.stream_only
        self.temperature = p.temperature if p.temperature is not None else d.get("temperature")
        self.max_tokens = int(p.max_tokens if p.max_tokens is not None else d.get("max_tokens") or 8192)
        self.timeout = float(p.timeout if p.timeout is not None else d.get("timeout") or 120.0)
        self.verify_ssl = bool(p.verify_ssl if p.verify_ssl is not None else d.get("verify_ssl", True))

    # ---- 派生路径 ----
    @property
    def workspace_dir(self) -> Path:
        """agent 私有工作区（记忆、todo、skills 等），与项目目录分开。"""
        return self.home_dir / "workspace"

    @property
    def log_dir(self) -> Path:
        return self.home_dir / "logs"

    @property
    def audit_log_path(self) -> Path:
        return self.home_dir / "audit.jsonl"

    @property
    def history_path(self) -> Path:
        return self.home_dir / "history"

    @property
    def state_path(self) -> Path:
        return self.home_dir / "state.json"

    @property
    def skills_dirs(self) -> list[Path]:
        """技能目录：项目级优先，其次用户级。不存在的目录会被 SDK 跳过。"""
        return [self.project_dir / ".mole" / "skills", self.home_dir / "skills"]

    @property
    def mcp_config_paths(self) -> list[Path]:
        return [self.project_dir / ".mcp.json", self.home_dir / "mcp.json"]

    def validate(self) -> list[str]:
        """返回配置问题列表；空列表表示可以启动。"""
        problems: list[str] = []
        if self.model_error:
            problems.append(self.model_error)
        elif not self.model:
            problems.append("没有可用的模型：复制 models.example.toml 为 models.toml 并填写（或在 .env 里设置 MOLE_MODEL 等）")
        else:
            if not self.api_base:
                problems.append(f"{self.model_spec} 缺少 api_base")
            if self.auth == "api_key" and not self.api_key and self.provider != "OpenAIAccount":
                problems.append(f"{self.model_spec} 缺少 API key")
        if self.language not in {"cn", "en"}:
            problems.append(f"MOLE_LANGUAGE 只能是 cn 或 en，当前为 {self.language!r}")
        if not self.project_dir.is_dir():
            problems.append(f"项目目录不存在：{self.project_dir}")
        return problems


def model_config_paths(home_dir: Path) -> list[Path]:
    return [home_dir / "models.toml", PACKAGE_ROOT / "models.toml"]


def load_settings(
    project_dir: str | Path | None = None,
    *,
    verbose: bool = False,
    model: Optional[str] = None,
) -> Settings:
    """model：命令行指定的 供应商/模型；为空时依次尝试上次使用的模型、models.toml 的 default。"""
    load_dotenv(PACKAGE_ROOT / ".env", override=False)
    load_dotenv(_home_dir() / ".env", override=False)

    extra_deny = _get_list("EXTRA_BASH_DENY", [], sep=";;")

    settings = Settings(
        temperature=_get_float("TEMPERATURE", None),
        max_tokens=_get_int("MAX_TOKENS", 8192),
        timeout=_get_float("TIMEOUT", 120.0) or 120.0,
        verify_ssl=_get_bool("VERIFY_SSL", True),
        agent_name=_get("AGENT_NAME", "Mole") or "Mole",
        language=_get("LANGUAGE", "cn").lower() or "cn",
        max_iterations=_get_int("MAX_ITERATIONS", 40),
        project_dir=Path(project_dir or os.getcwd()).expanduser().resolve(),
        home_dir=_home_dir(),
        confirm_tools=_get_list("CONFIRM_TOOLS", ["write_file", "edit_file", "bash", "powershell"]),
        restrict_to_project=_get_bool("RESTRICT_TO_PROJECT", True),
        bash_deny_patterns=[*DEFAULT_BASH_DENY, *extra_deny],
        enable_web=_get_bool("ENABLE_WEB", False),
        enable_context_rails=_get_bool("ENABLE_CONTEXT_RAILS", True),
        enable_subagents=_get_bool("ENABLE_SUBAGENTS", True),
        audit_log=_get_bool("AUDIT_LOG", True),
        verbose=verbose,
    )

    try:
        settings.catalog = load_catalog(model_config_paths(settings.home_dir))
        choice = _pick_startup_model(settings, model)
        settings.apply_choice(choice)
        problems = choice.provider.problems()
        if problems:
            settings.model_error = "；".join(problems)
    except ModelSelectionError as exc:
        settings.model_error = str(exc)
    return settings


def _pick_startup_model(settings: Settings, explicit: Optional[str]) -> ModelChoice:
    catalog = settings.catalog
    if explicit:
        return catalog.resolve(explicit)  # 命令行写错了要直接报错
    last = load_last_model(settings.state_path)
    if last:
        try:
            return catalog.resolve(last)
        except ModelSelectionError:
            pass  # 上次的模型已从配置里删掉，回落到 default
    return catalog.resolve(None)

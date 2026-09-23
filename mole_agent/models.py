"""多供应商 / 多模型目录。

配置文件 models.toml（按顺序查找，同名供应商以先找到的为准）：
    ~/.mole-agent/models.toml  →  <本项目根目录>/models.toml

.env 里旧的单模型配置（MOLE_PROVIDER / MOLE_API_BASE / MOLE_API_KEY / MOLE_MODEL）会作为一个
名为 "default" 的供应商加入目录；没有 models.toml 时它就是默认模型，保持旧配置可用。

选择模型的写法（命令行 -m 和 REPL 里的 /model 通用）：
    deepseek/deepseek-v4-pro    供应商/模型（模型名里本身带 / 也可以，只按第一个 / 切分）
    deepseek                    只写供应商 → 用它列表里的第一个模型
    deepseek-v4-pro             只写模型 → 在所有供应商的 models 列表里查找，唯一命中即可
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# openjiuwen 的 client_provider 取值（ProviderType）。协议层只有 OpenAI 兼容和 Anthropic 两套，
# 其余名字是 OpenAI 兼容协议上的厂商别名，框架会带上对应厂商的适配。
KNOWN_CLIENT_PROVIDERS = {
    "OpenAI", "Anthropic", "DeepSeek", "DashScope", "SiliconFlow", "OpenRouter", "Moonshot",
    "MiniMax", "ModelArts", "VolcEngine", "Qianfan", "Zhipu", "MiMo", "AscendAffinity",
    "InferenceAffinity", "OpenAIAccount",
}


class ModelSelectionError(ValueError):
    """模型选择写法有误，或目标供应商配置不完整。"""


# 鉴权方式（models.toml 里的 auth）→ openjiuwen 的 LLMAuthMode
#   api_key  默认：Authorization: Bearer <key>
#   headers  靠 headers 里的请求头鉴权（如 ModelArts 在线服务的 X-Auth-Token / X-Apig-AppCode），不发 Authorization
#   none     不鉴权（本地 vLLM 等）
AUTH_MODES = ("api_key", "headers", "none")
_AUTH_ALIASES = {
    "api_key": "api_key", "apikey": "api_key", "key": "api_key", "bearer": "api_key",
    "headers": "headers", "header": "headers", "custom_headers": "headers",
    "none": "none", "no_auth": "none",
}
# 请求头取值里的 ${VAR} 从环境变量（含 .env）读取，密钥不用写进 models.toml
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# openjiuwen 会丢弃自定义请求头里的这些键（header_utils.PROTECTED_HEADERS）
_DROPPED_HEADERS = {"authorization", "host", "content-length", "transfer-encoding", "connection"}


@dataclass
class ProviderConfig:
    name: str
    client_provider: str = "OpenAI"
    api_base: str = ""
    api_key: str = ""
    api_key_env: str = ""
    models: list[str] = field(default_factory=list)
    verify_ssl: Optional[bool] = None
    timeout: Optional[float] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    headers: dict[str, str] = field(default_factory=dict)   # 原样保存，值里可以有 ${VAR}
    auth: str = "api_key"                                    # 见 AUTH_MODES

    @property
    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env, "").strip()
        return ""

    @property
    def resolved_headers(self) -> dict[str, str]:
        """把请求头里的 ${VAR} 换成环境变量的值；变量没设置时替换成空串（problems() 会报出来）。"""
        return {
            key: _ENV_REF_RE.sub(lambda m: os.getenv(m.group(1), "").strip(), value)
            for key, value in self.headers.items()
        }

    def missing_env(self) -> list[str]:
        """还没设置的环境变量：API key 的，以及请求头里 ${VAR} 引用的。"""
        missing: list[str] = []
        if self.auth == "api_key" and not self.api_key and self.api_key_env and not self.resolved_api_key:
            missing.append(self.api_key_env)
        for value in self.headers.values():
            for var in _ENV_REF_RE.findall(value):
                if not os.getenv(var, "").strip() and var not in missing:
                    missing.append(var)
        return missing

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.api_base:
            out.append(f"供应商 {self.name} 缺少 api_base")
        if self.auth == "api_key" and not self.resolved_api_key and self.client_provider != "OpenAIAccount":
            hint = f"请设置环境变量 {self.api_key_env}" if self.api_key_env else "请配置 api_key 或 api_key_env"
            out.append(f"供应商 {self.name} 缺少 API key（{hint}；不用 key、靠请求头鉴权的写 auth = \"headers\"）")
        if self.auth == "headers" and not self.headers:
            out.append(f"供应商 {self.name} 设置了 auth = \"headers\"，但没有配置 headers")
        dropped = [k for k in self.headers if k.lower() in _DROPPED_HEADERS]
        if dropped:
            out.append(
                f"供应商 {self.name} 的请求头 {'、'.join(dropped)} 会被 openjiuwen 丢弃，不会发出去；"
                "Bearer 鉴权请用 api_key_env，其他鉴权方式请改用网关支持的专用请求头"
            )
        header_vars = [v for v in self.missing_env() if v != self.api_key_env]
        if header_vars:
            out.append(f"供应商 {self.name} 的请求头需要环境变量 {'、'.join(header_vars)}（在 .env 里设置）")
        return out


@dataclass
class ModelChoice:
    provider: ProviderConfig
    model: str

    @property
    def spec(self) -> str:
        return f"{self.provider.name}/{self.model}"


@dataclass
class ModelCatalog:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    default: str = ""
    sources: list[Path] = field(default_factory=list)
    # 子 agent 专用模型：{子 agent 名: "供应商/模型"}；没配置的子 agent 跟随主 agent 当前模型
    subagent_models: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.providers

    def all_choices(self) -> list[ModelChoice]:
        return [ModelChoice(p, m) for p in self.providers.values() for m in p.models]

    def resolve(self, spec: Optional[str]) -> ModelChoice:
        spec = (spec or "").strip() or self.default.strip()
        if not self.providers:
            raise ModelSelectionError("还没有配置任何模型供应商：复制 models.example.toml 为 models.toml 后填写")
        if not spec:
            first = next(iter(self.providers.values()))
            if not first.models:
                raise ModelSelectionError(f"供应商 {first.name} 没有配置 models，请在 models.toml 里设置 default")
            return ModelChoice(first, first.models[0])

        head, sep, tail = spec.partition("/")
        if head in self.providers:
            provider = self.providers[head]
            if sep and tail:
                return ModelChoice(provider, tail)
            if not provider.models:
                raise ModelSelectionError(f"供应商 {head} 没有配置 models，请写成 {head}/<模型名>")
            return ModelChoice(provider, provider.models[0])

        hits = [c for c in self.all_choices() if c.model == spec]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            options = "、".join(c.spec for c in hits)
            raise ModelSelectionError(f"模型 {spec} 在多个供应商下都有，请写全：{options}")
        known = "、".join(self.providers) or "（无）"
        raise ModelSelectionError(f"找不到 {spec!r}。已配置的供应商：{known}；写法：供应商/模型")


def _parse_provider(name: str, raw: dict[str, Any]) -> ProviderConfig:
    client_provider = str(raw.get("client_provider") or raw.get("type") or "OpenAI")
    if client_provider not in KNOWN_CLIENT_PROVIDERS:
        raise ModelSelectionError(
            f"models.toml 中供应商 {name} 的 client_provider={client_provider!r} 不被 openjiuwen 支持，"
            f"可选：{', '.join(sorted(KNOWN_CLIENT_PROVIDERS))}"
        )
    models = raw.get("models") or []
    if isinstance(models, str):
        models = [models]
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        raise ModelSelectionError(f"models.toml 中供应商 {name} 的 headers 应写成表，例如 headers = {{ \"X-Auth-Token\" = \"${{TOKEN}}\" }}")
    api_key = str(raw.get("api_key", "")).strip()
    api_key_env = str(raw.get("api_key_env", "")).strip()
    raw_auth = str(raw.get("auth") or "").strip().lower()
    if raw_auth:
        auth = _AUTH_ALIASES.get(raw_auth)
        if auth is None:
            raise ModelSelectionError(
                f"models.toml 中供应商 {name} 的 auth={raw_auth!r} 不支持，可选：{'、'.join(AUTH_MODES)}"
            )
    else:
        # 没写 auth：只配了请求头、没配任何 key 时，按请求头鉴权处理
        auth = "headers" if headers and not api_key and not api_key_env else "api_key"
    return ProviderConfig(
        name=name,
        client_provider=client_provider,
        api_base=str(raw.get("api_base", "")).strip(),
        api_key=api_key,
        api_key_env=api_key_env,
        models=[str(m) for m in models],
        verify_ssl=raw.get("verify_ssl"),
        timeout=raw.get("timeout"),
        max_tokens=raw.get("max_tokens"),
        temperature=raw.get("temperature"),
        headers={str(k): str(v) for k, v in headers.items()},
        auth=auth,
    )


def load_catalog(paths: list[Path]) -> ModelCatalog:
    catalog = ModelCatalog()
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ModelSelectionError(f"{path} 解析失败：{exc}") from exc
        catalog.sources.append(path)
        if not catalog.default and data.get("default"):
            catalog.default = str(data["default"])
        subagents = data.get("subagents")
        if isinstance(subagents, dict):
            for agent_name, spec in subagents.items():
                catalog.subagent_models.setdefault(str(agent_name), str(spec))
        for name, raw in (data.get("providers") or {}).items():
            if name not in catalog.providers and isinstance(raw, dict):
                catalog.providers[name] = _parse_provider(name, raw)

    # .env 里旧的单模型配置（MOLE_MODEL 等）继续可用：作为名为 default 的供应商加入目录
    legacy = _legacy_env_provider()
    if legacy is not None and legacy.name not in catalog.providers:
        catalog.providers[legacy.name] = legacy
        if not catalog.default:
            catalog.default = f"{legacy.name}/{legacy.models[0]}"
    return catalog


def _legacy_env_provider() -> Optional[ProviderConfig]:
    """兼容旧的单模型配置（.env 里的 MOLE_*）。"""
    model = os.getenv("MOLE_MODEL", "").strip()
    if not model:
        return None
    return ProviderConfig(
        name="default",
        client_provider=os.getenv("MOLE_PROVIDER", "OpenAI").strip() or "OpenAI",
        api_base=os.getenv("MOLE_API_BASE", "").strip(),
        api_key_env="MOLE_API_KEY",
        models=[model],
    )


# --------------------------------------------------------------------------- #
# 上次使用的模型（/model 切换后记住，下次启动沿用）
# --------------------------------------------------------------------------- #
def load_last_model(state_path: Path) -> str:
    try:
        return str(json.loads(state_path.read_text(encoding="utf-8")).get("last_model", ""))
    except (OSError, ValueError):
        return ""


def save_last_model(state_path: Path, spec: str) -> None:
    try:
        state: dict[str, Any] = {}
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_model"] = spec
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# 在线查询供应商当前可用的模型
# --------------------------------------------------------------------------- #
async def list_remote_models(provider: ProviderConfig, *, timeout: float = 15.0) -> list[str]:
    """调用供应商的 /models 接口。OpenAI 兼容：GET {api_base}/models；Anthropic：GET {base}/v1/models。"""
    import httpx

    key = provider.resolved_api_key if provider.auth != "none" else ""
    headers = provider.resolved_headers
    base = provider.api_base.rstrip("/")
    if provider.client_provider == "Anthropic":
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/v1/models"
        headers["anthropic-version"] = "2023-06-01"
        if key and provider.auth == "api_key":
            headers["x-api-key"] = key
    else:
        url = f"{base}/models"
        if key and provider.auth == "api_key":  # 请求头鉴权时和模型调用一样不发 Authorization
            headers["Authorization"] = f"Bearer {key}"

    verify = True if provider.verify_ssl is None else bool(provider.verify_ssl)
    async with httpx.AsyncClient(timeout=timeout, verify=verify, trust_env=True) as client:
        resp = await client.get(url, headers=headers, params={"limit": 1000} if provider.client_provider == "Anthropic" else None)
        resp.raise_for_status()
        payload = resp.json()
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    ids = [str(item.get("id")) for item in items or [] if isinstance(item, dict) and item.get("id")]
    return sorted(set(ids))

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
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env, "").strip()
        return ""

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.api_base:
            out.append(f"供应商 {self.name} 缺少 api_base")
        if not self.resolved_api_key and self.client_provider != "OpenAIAccount":
            hint = f"请设置环境变量 {self.api_key_env}" if self.api_key_env else "请配置 api_key 或 api_key_env"
            out.append(f"供应商 {self.name} 缺少 API key（{hint}）")
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
    return ProviderConfig(
        name=name,
        client_provider=client_provider,
        api_base=str(raw.get("api_base", "")).strip(),
        api_key=str(raw.get("api_key", "")).strip(),
        api_key_env=str(raw.get("api_key_env", "")).strip(),
        models=[str(m) for m in models],
        verify_ssl=raw.get("verify_ssl"),
        timeout=raw.get("timeout"),
        max_tokens=raw.get("max_tokens"),
        temperature=raw.get("temperature"),
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
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

    key = provider.resolved_api_key
    headers = dict(provider.headers)
    base = provider.api_base.rstrip("/")
    if provider.client_provider == "Anthropic":
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/v1/models"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        url = f"{base}/models"
        if key:
            headers["Authorization"] = f"Bearer {key}"

    verify = True if provider.verify_ssl is None else bool(provider.verify_ssl)
    async with httpx.AsyncClient(timeout=timeout, verify=verify, trust_env=True) as client:
        resp = await client.get(url, headers=headers, params={"limit": 1000} if provider.client_provider == "Anthropic" else None)
        resp.raise_for_status()
        payload = resp.json()
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    ids = [str(item.get("id")) for item in items or [] if isinstance(item, dict) and item.get("id")]
    return sorted(set(ids))

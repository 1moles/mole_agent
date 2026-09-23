"""连接诊断：mole --check 调用模型失败时，找出是哪一步连不上。

openjiuwen 报的 APIConnectionError 只有一句 "Connection error"，真正的原因（DNS、代理、证书、TLS 协商）
藏在异常链里。这里做两件事：
1. root_cause()：沿 __cause__ / __context__ 找到最底层的异常；
2. diagnose()：按 openjiuwen 的实际行为逐步检查——用哪个代理、NO_PROXY 是否生效、DNS、TCP、
   代理 CONNECT、TLS 握手（用和 SDK 相同的严格 TLS 配置）。

注意 openjiuwen 选代理的规则和 curl 不完全一样（见 sdk_proxy_for / no_proxy_warnings），
所以「curl 能通、mole 不通」很常见。
"""

from __future__ import annotations

import os
import socket
import ssl
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

PROXY_ENV_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
NO_PROXY_ENV_VARS = ("NO_PROXY", "no_proxy")
CONNECT_TIMEOUT = 5.0


@dataclass
class Step:
    ok: Optional[bool]   # True 通过 / False 失败 / None 提示信息
    title: str
    detail: str = ""

    def render(self) -> str:
        mark = {True: "✓", False: "✗", None: "·"}[self.ok]
        return f"{mark} {self.title}" + (f"：{self.detail}" if self.detail else "")


def root_cause(exc: BaseException) -> BaseException:
    """异常链最底层的那个（FrameworkError → APIConnectionError → httpx.ConnectError → SSLError …）。"""
    seen: set[int] = set()
    current = exc
    while id(current) not in seen:
        seen.add(id(current))
        nxt = current.__cause__ or current.__context__
        if nxt is None:
            break
        current = nxt
    return current


_CONNECTION_ERROR_NAMES = {
    "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ProxyError",
    "SSLError", "SSLCertVerificationError", "RemoteProtocolError",
}


def is_connection_error(exc: BaseException) -> bool:
    """异常链里有没有连接层面的错误（相对于 401 / 404 这类服务端已经回应了的错误）。"""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _CONNECTION_ERROR_NAMES or isinstance(current, (ssl.SSLError, ConnectionError,
                                                                                         socket.gaierror, TimeoutError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def describe_exception_chain(exc: BaseException, limit: int = 6) -> list[str]:
    lines: list[str] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen and len(lines) < limit:
        seen.add(id(current))
        text = str(current).strip().replace("\n", " ")
        lines.append(f"{type(current).__module__}.{type(current).__name__}: {text[:300]}")
        current = current.__cause__ or current.__context__
    return lines


def mask_proxy(url: str) -> str:
    """代理地址里可能带账号密码（http://user:pass@proxy:8080），显示前打码。"""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***@{host}{port}"
    return url


def sdk_proxy_for(url: str) -> Optional[str]:
    """openjiuwen 访问这个地址时实际使用的代理（它自己的规则，不是 curl / httpx 的规则）。"""
    try:
        from openjiuwen.core.common.security.url_utils import UrlUtils

        return UrlUtils.get_global_proxy_url(url)
    except Exception:  # noqa: BLE001 —— SDK 内部实现变化时退回到近似规则
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            if os.getenv(name):
                return os.getenv(name, "").strip()
        return None


def httpx_env_proxy_for(url: str) -> Optional[str]:
    """openjiuwen 没选代理时，httpx（trust_env=True）会按环境变量自己选：它还认 ALL_PROXY，NO_PROXY 规则也更宽。"""
    try:
        import httpx
        from httpx._utils import URLPattern, get_environment_proxies

        mounts = get_environment_proxies()
        target = httpx.URL(url)
        for pattern in sorted(URLPattern(key) for key in mounts):  # httpx 自己也按这个顺序匹配，越具体越靠前
            if pattern.matches(target):
                return mounts[pattern.pattern]
        return None
    except Exception:  # noqa: BLE001 —— httpx 内部实现变化时退回到标准库的近似规则
        import urllib.request

        host = urlparse(url).hostname or ""
        if urllib.request.proxy_bypass_environment(host):
            return None
        proxies = urllib.request.getproxies()
        return proxies.get(urlparse(url).scheme) or proxies.get("all")


def effective_proxy(url: str) -> tuple[Optional[str], str]:
    """返回 (实际使用的代理, 由谁选的)。openjiuwen 选了就用它的；否则 httpx 按环境变量再选一次。"""
    proxy = sdk_proxy_for(url)
    if proxy:
        return proxy, "openjiuwen"
    proxy = httpx_env_proxy_for(url)
    return (proxy, "httpx") if proxy else (None, "")


def _no_proxy_entries() -> list[str]:
    entries: list[str] = []
    for name in NO_PROXY_ENV_VARS:
        for item in os.getenv(name, "").replace(";", ",").replace(" ", ",").split(","):
            item = item.strip().lower()
            if item and item not in entries:
                entries.append(item)
    return entries


def no_proxy_warnings(host: str) -> list[str]:
    """NO_PROXY 里「curl 认、openjiuwen 不认」的写法。"""
    host = host.lower()
    warnings: list[str] = []
    for entry in _no_proxy_entries():
        bare = entry.lstrip("*").lstrip(".")
        if entry.startswith("*.") or (entry.startswith("*") and entry != "*"):
            if host == bare or host.endswith("." + bare):
                warnings.append(f"NO_PROXY 里的 {entry}：openjiuwen 不支持通配符，要写成 .{bare}")
        elif not entry.startswith(".") and host.endswith("." + entry):
            warnings.append(f"NO_PROXY 里的 {entry}：openjiuwen 只做精确匹配，不包含子域名 {host}，要写成 .{entry}")
    return warnings


def _classify_ssl_error(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return ("证书校验失败。常见原因：服务用的是公司内部 CA 签发的证书，而这台机器的 Python 不信任它；"
                "或者是 python.org 安装包装的 Python，没运行过「Install Certificates.command」。"
                "可以把内部根证书加到系统/Python 的信任库，或临时在 models.toml 该供应商下写 verify_ssl = false")
    if any(key in text for key in ("HANDSHAKE_FAILURE", "NO_CIPHERS", "NO_SHARED_CIPHER", "UNSUPPORTED_PROTOCOL",
                                   "WRONG_VERSION_NUMBER", "TLSV1_ALERT_PROTOCOL_VERSION")):
        return f"TLS 协商失败（{text[:160]}）"
    return text[:200]


def _connect(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def _proxy_connect(proxy_url: str, host: str, port: int, timeout: float) -> tuple[Optional[socket.socket], Step]:
    """通过 HTTP 代理发 CONNECT，返回打通后的 socket。"""
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    phost, pport = parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 8080)
    if parsed.scheme not in ("http", ""):
        return None, Step(None, "代理 CONNECT", f"代理协议是 {parsed.scheme}，跳过这一步的检查")
    try:
        sock = _connect(phost, pport, timeout)
    except OSError as exc:
        return None, Step(False, f"连接代理 {phost}:{pport}", str(exc))
    request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if parsed.username:
        import base64

        token = base64.b64encode(f"{parsed.username}:{parsed.password or ''}".encode()).decode()
        request += f"Proxy-Authorization: Basic {token}\r\n"
    try:
        sock.sendall((request + "\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    except OSError as exc:
        sock.close()
        return None, Step(False, "代理 CONNECT", str(exc))
    status_line = response.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = status_line.split()
    if len(parts) >= 2 and parts[1] == "200":
        return sock, Step(True, f"代理 CONNECT {host}:{port}", status_line)
    sock.close()
    hint = {"407": "代理需要认证", "403": "代理拒绝访问这个地址", "502": "代理连不上目标地址（它那边的网络也不通）"}
    code = parts[1] if len(parts) >= 2 else ""
    return None, Step(False, f"代理 CONNECT {host}:{port}", f"{status_line or '无响应'} {hint.get(code, '')}".strip())


def diagnose(api_base: str, verify_ssl: bool = True, timeout: float = CONNECT_TIMEOUT) -> list[Step]:
    """按 openjiuwen 的实际行为逐步检查到 api_base 的连通性。只读，不发业务请求。"""
    steps: list[Step] = []
    parsed = urlparse(api_base)
    host = parsed.hostname or ""
    if not host or parsed.scheme not in ("http", "https"):
        return [Step(False, "api_base", f"不是合法的 http(s) 地址：{api_base!r}")]
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    steps.append(Step(None, "目标", f"{parsed.scheme}://{host}:{port}"))

    # 1. 代理
    env_set = [f"{n}={mask_proxy(os.getenv(n, ''))}" for n in PROXY_ENV_VARS if os.getenv(n)]
    no_proxy = [f"{n}={os.getenv(n)}" for n in NO_PROXY_ENV_VARS if os.getenv(n)]
    if env_set or no_proxy:
        steps.append(Step(None, "代理环境变量", "；".join(env_set + no_proxy)))
    proxy, chooser = effective_proxy(api_base)
    if chooser == "openjiuwen":
        steps.append(Step(None, "会走代理", mask_proxy(proxy or "")
                          + "（openjiuwen 按 http_proxy → https_proxy → HTTP_PROXY → HTTPS_PROXY 取第一个非空的，"
                            "https 地址也可能用 http_proxy；内网地址不该走代理时，把域名写进 NO_PROXY）"))
    elif chooser == "httpx":
        steps.append(Step(None, "会走代理", mask_proxy(proxy or "")
                          + "（openjiuwen 没选代理，但底层 httpx 按环境变量（含 ALL_PROXY）选了这个）"))
    else:
        steps.append(Step(None, "直连，不走代理"))
    for warning in no_proxy_warnings(host):
        steps.append(Step(False, "NO_PROXY 写法", warning))

    # 2. DNS（走代理时由代理解析目标，本机解析不了不一定是问题）
    try:
        addrs = sorted({info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        steps.append(Step(True, f"DNS 解析 {host}", "、".join(addrs[:4])))
    except OSError as exc:
        if not proxy:
            steps.append(Step(False, f"DNS 解析 {host}", f"{exc}（不在内网 / 没连 VPN / DNS 配置不同？）"))
            return steps
        steps.append(Step(None, f"DNS 解析 {host}", f"本机解析失败（{exc}），走代理时由代理解析"))

    # 3. TCP（直连或经代理）
    sock: Optional[socket.socket] = None
    if proxy and parsed.scheme == "http":
        # http 地址经代理是普通转发（不发 CONNECT），能连上代理就只能查到这一步
        pp = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        try:
            _connect(pp.hostname or "", pp.port or 8080, timeout).close()
            steps.append(Step(True, f"TCP 连接代理 {pp.hostname}:{pp.port or 8080}",
                              "http 地址由代理转发，代理之后是否能通要看代理日志；内网地址通常应该加进 NO_PROXY"))
        except OSError as exc:
            steps.append(Step(False, f"TCP 连接代理 {pp.hostname}:{pp.port or 8080}", str(exc)))
        return steps
    if proxy:
        sock, step = _proxy_connect(proxy, host, port, timeout)
        steps.append(step)
    else:
        try:
            sock = _connect(host, port, timeout)
            steps.append(Step(True, f"TCP 连接 {host}:{port}"))
        except OSError as exc:
            steps.append(Step(False, f"TCP 连接 {host}:{port}", f"{exc}（防火墙 / 端口 / 网络隔离？）"))
    if sock is None:
        return steps

    # 4. TLS：和 openjiuwen 一样的严格配置（TLS1.2+，TLS1.2 下只允许 ECDHE + AES-GCM）
    with sock:
        if parsed.scheme != "https":
            steps.append(Step(True, "HTTP 明文连接，不涉及 TLS"))
            return steps
        sock.settimeout(timeout)
        try:
            ctx = _sdk_ssl_context(verify_ssl)
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                steps.append(Step(True, "TLS 握手", f"{tls.version()} {tls.cipher()[0] if tls.cipher() else ''}"
                                  + ("" if verify_ssl else "（verify_ssl = false，未校验证书）")))
        except (ssl.SSLError, OSError) as exc:
            detail = _classify_ssl_error(exc)
            if "TLS 协商失败" in detail and _default_tls_works(host, port, proxy, timeout):
                detail += ("；用 Python 默认 TLS 配置可以握手，说明服务端不支持 openjiuwen 限定的加密套件"
                           "（TLS1.2 下只允许 ECDHE + AES-GCM），需要服务端开启这些套件或升级到 TLS1.3")
            steps.append(Step(False, "TLS 握手", detail))
    return steps


def _sdk_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        from openjiuwen.core.common.security.ssl_utils import SslUtils

        return SslUtils.create_strict_ssl_context(None)
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _default_tls_works(host: str, port: int, proxy: Optional[str], timeout: float) -> bool:
    try:
        if proxy:
            sock, _ = _proxy_connect(proxy, host, port, timeout)
            if sock is None:
                return False
        else:
            sock = _connect(host, port, timeout)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with sock, ctx.wrap_socket(sock, server_hostname=host):
            return True
    except (ssl.SSLError, OSError):
        return False

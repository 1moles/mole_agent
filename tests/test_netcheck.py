"""连接诊断（mole --check 失败时）：异常链、代理选择（openjiuwen 与 httpx 的差异）、NO_PROXY 写法、
DNS / TCP / 代理 CONNECT / TLS 各步骤。全部用本机临时服务，不联网。"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mole_agent import netcheck

_PROXY_VARS = (*netcheck.PROXY_ENV_VARS, *netcheck.NO_PROXY_ENV_VARS)


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def _failed(steps) -> list[str]:
    return [s.render() for s in steps if s.ok is False]


# ---------------------------------------------------------------- 异常链
class APIConnectionError(Exception):  # 名字与 openai 的一致即可
    pass


def _chained() -> Exception:
    try:
        try:
            try:
                raise ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate")
            except ssl.SSLError as exc:
                raise ConnectionError("ConnectError: tls failed") from exc
        except ConnectionError as exc:
            raise APIConnectionError("Connection error.") from exc
    except APIConnectionError as exc:
        try:
            raise RuntimeError("[181001] model call failed, reason: openAI API async stream error") from exc
        except RuntimeError as outer:
            return outer


def test_exception_chain_shows_root_cause():
    exc = _chained()
    assert isinstance(netcheck.root_cause(exc), ssl.SSLCertVerificationError)
    chain = netcheck.describe_exception_chain(exc)
    assert len(chain) == 4 and "unable to get local issuer certificate" in chain[-1]
    assert netcheck.is_connection_error(exc)
    assert not netcheck.is_connection_error(ValueError("401 Unauthorized"))


# ---------------------------------------------------------------- 代理选择
def test_sdk_prefers_http_proxy_even_for_https(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://p-http:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://p-https:2")
    assert netcheck.effective_proxy("https://llm.inner.com/v1") == ("http://p-http:1", "openjiuwen")


def test_no_proxy_without_leading_dot_does_not_cover_subdomains(monkeypatch):
    """curl 里 NO_PROXY=inner.com 包含 llm.inner.com；openjiuwen 不包含——同一台机器 curl 通、mole 不通。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    monkeypatch.setenv("NO_PROXY", "inner.com,*.corp.net,.ok.com")
    assert netcheck.effective_proxy("https://llm.inner.com/v1")[1] == "openjiuwen"      # 仍然走代理
    assert netcheck.effective_proxy("https://llm.ok.com/v1") == (None, "")             # 带点的写法生效
    warnings = netcheck.no_proxy_warnings("llm.inner.com")
    assert warnings and ".inner.com" in warnings[0]
    assert any("通配符" in w for w in netcheck.no_proxy_warnings("gw.corp.net"))
    assert netcheck.no_proxy_warnings("llm.ok.com") == []


def test_all_proxy_is_used_by_httpx_even_though_sdk_ignores_it(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://proxy.corp:1080")
    assert netcheck.sdk_proxy_for("https://llm.inner.com/v1") is None
    assert netcheck.effective_proxy("https://llm.inner.com/v1") == ("http://proxy.corp:1080", "httpx")


def test_proxy_credentials_are_masked():
    assert netcheck.mask_proxy("http://alice:s3cret@proxy.corp:8080") == "http://***@proxy.corp:8080"
    assert netcheck.mask_proxy("http://proxy.corp:8080") == "http://proxy.corp:8080"


# ---------------------------------------------------------------- DNS / TCP
class _Ok(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()


@pytest.fixture()
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_diagnose_reachable_http(http_server):
    steps = netcheck.diagnose(f"http://127.0.0.1:{http_server}/v1")
    assert _failed(steps) == []
    assert any("TCP 连接" in s.title and s.ok for s in steps)


def test_diagnose_closed_port():
    port = _free_port()
    failed = _failed(netcheck.diagnose(f"http://127.0.0.1:{port}/v1", timeout=2))
    assert failed and "TCP 连接" in failed[0]


def test_diagnose_unresolvable_host():
    failed = _failed(netcheck.diagnose("https://no-such-host.invalid/v1", timeout=2))
    assert failed and "DNS 解析" in failed[0]


def test_diagnose_bad_api_base():
    assert "不是合法的" in _failed(netcheck.diagnose("api.example.com/v1"))[0]


# ---------------------------------------------------------------- 代理 CONNECT
def _fake_proxy(response: bytes):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.recv(4096)
                conn.sendall(response)

    threading.Thread(target=serve, daemon=True).start()
    return listener


def test_proxy_refusing_connect_is_reported(monkeypatch):
    listener = _fake_proxy(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    try:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{listener.getsockname()[1]}")
        failed = _failed(netcheck.diagnose("https://llm.inner.example/v1", timeout=2))
        assert failed and "代理 CONNECT" in failed[-1] and "403" in failed[-1] and "拒绝" in failed[-1]
    finally:
        listener.close()


# ---------------------------------------------------------------- TLS：证书、加密套件
@pytest.fixture(scope="module")
def self_signed(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("没有 openssl 命令，无法生成测试证书")
    d = tmp_path_factory.mktemp("tls")
    key, cert = d / "key.pem", d / "cert.pem"
    result = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
         "-keyout", str(key), "-out", str(cert)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(f"生成测试证书失败：{result.stderr[:200]!r}")
    return key, cert


def _tls_server(key: Path, cert: Path, configure=None) -> socket.socket:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    if configure:
        configure(ctx)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=serve, daemon=True).start()
    return listener


def test_untrusted_certificate_is_explained(self_signed):
    listener = _tls_server(*self_signed)
    try:
        url = f"https://localhost:{listener.getsockname()[1]}/v1"
        failed = _failed(netcheck.diagnose(url, verify_ssl=True, timeout=3))
        assert failed and "证书校验失败" in failed[-1] and "verify_ssl = false" in failed[-1]
        assert _failed(netcheck.diagnose(url, verify_ssl=False, timeout=3)) == []   # 不校验证书就能握手
    finally:
        listener.close()


def test_cipher_mismatch_is_explained(self_signed):
    """服务端只支持 TLS1.2 的 RSA 密钥交换套件：Python 默认配置能握手，openjiuwen 的严格配置不行。"""
    def legacy_only(ctx: ssl.SSLContext) -> None:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_ciphers("AES256-GCM-SHA384:AES128-GCM-SHA256")
        except ssl.SSLError:
            pytest.skip("本机 OpenSSL 不支持这些测试套件")

    listener = _tls_server(*self_signed, configure=legacy_only)
    try:
        url = f"https://localhost:{listener.getsockname()[1]}/v1"
        failed = _failed(netcheck.diagnose(url, verify_ssl=False, timeout=3))
        if not failed:
            pytest.skip("本机 OpenSSL 与测试套件的组合没有触发协商失败")
        assert "TLS 协商失败" in failed[-1] or "TLS" in failed[-1]
    finally:
        listener.close()


# ---------------------------------------------------------------- mole --check 集成
async def test_check_prints_diagnosis_on_connection_error(tmp_path, capsys):
    from mole_agent import cli
    from mole_agent.config import Settings

    port = _free_port()
    settings = Settings(model="m", api_key="k", api_base=f"http://127.0.0.1:{port}/v1",
                        project_dir=tmp_path, home_dir=tmp_path / "home", timeout=5)
    assert await cli._check(settings) == 1
    out = capsys.readouterr().out
    assert "网络诊断" in out and "TCP 连接" in out and "✗" in out
    assert "异常链" in out

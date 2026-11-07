#!/usr/bin/env python3
"""Loopback-only HTTP service for the Bot Manager dashboard."""

import argparse
import base64
import binascii
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parent
PANEL_FILE = ROOT / "panel.html"
AGENT_FILE = ROOT / "server-agent.py"
MONITOR_FILE = ROOT / "server-monitor.py"
ALLOWED_LOG_SERVICES = {"astrbot", "snowluma", "novnc", "onebot"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
CREDENTIAL_BACKUP_KEEP = 8
SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
# 面板登录防护：连续失败后指数退避锁定；校验结果短时缓存，避免每个请求都算一次 scrypt。
AUTH_FAILURE_LIMIT = 5
AUTH_LOCK_BASE_SECONDS = 60
AUTH_LOCK_MAX_SECONDS = 900
AUTH_CACHE_SECONDS = 60
AUTH_CACHE_MAX = 32
AUTH_MAX_PARALLEL = 12


def running_as_root():
    """仅在 POSIX 上判断；Windows 上本地跑测试时按非 root 处理。"""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid) and geteuid() == 0
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-src http://127.0.0.1:*; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def load_monitor_module():
    spec = importlib.util.spec_from_file_location("server_monitor", MONITOR_FILE)
    if not spec or not spec.loader:
        raise RuntimeError("unable to load monitoring module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MONITOR = load_monitor_module()


class CredentialVerifier:
    """面板登录口令校验。文件被改写后按 mtime 自动重载，改密不必重启服务。"""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime = None
        self.username = b""
        self.salt = b""
        self.expected = b""
        self._read()

    def _read(self):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        username = str(payload["username"]).encode("utf-8")
        salt = bytes.fromhex(payload["salt"])
        expected = bytes.fromhex(payload["hash"])
        if not username or len(salt) < 16 or len(expected) < 32:
            raise ValueError("invalid credential file")
        self.username = username
        self.salt = salt
        self.expected = expected
        try:
            self._mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime = None

    def refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._mtime:
            return
        with self._lock:
            try:
                self._read()
            except (OSError, ValueError, KeyError, TypeError):
                # 文件正在替换或内容不合法时保留旧口令，避免把面板直接锁死。
                pass

    def describe(self):
        self.refresh()
        try:
            updated = int(self.path.stat().st_mtime)
        except OSError:
            updated = 0
        return {
            "username": self.username.decode("utf-8"),
            "updatedAt": updated,
        }

    def verify(self, authorization):
        self.refresh()
        if not authorization or not authorization.startswith("Basic "):
            return False
        try:
            encoded = authorization[6:].strip()
            if len(encoded) > 1024:
                return False
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=self.salt,
            **SCRYPT_PARAMS,
        )
        return hmac.compare_digest(
            username.encode("utf-8"), self.username
        ) and hmac.compare_digest(actual, self.expected)


def prune_credential_backups(path):
    backups = sorted(Path(path).parent.glob(f"{Path(path).name}.bak.*"))
    for item in backups[:-CREDENTIAL_BACKUP_KEEP]:
        try:
            item.unlink()
        except OSError:
            pass


def write_panel_credentials(path, username, password):
    """把新口令写成 scrypt 摘要；返回可回传的最小描述，不包含任何密钥材料。"""
    path = Path(path)
    username = "" if username is None else str(username).strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名需为 1-64 位字母、数字、点、下划线或短横线")
    if not isinstance(password, str):
        raise ValueError("密码格式不正确")
    password = password.strip()
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"密码长度需在 {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} 之间"
        )
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT_PARAMS)
    payload = {"username": username, "salt": salt.hex(), "hash": digest.hex()}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        try:
            shutil.copyfile(path, backup)
            os.chmod(backup, 0o600)
        except OSError:
            pass
        prune_credential_backups(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if running_as_root():
        os.chown(path, 0, 0)
    return {"username": username, "updatedAt": int(time.time())}


class AuthGuard:
    """面板登录的失败限速、临时锁定与短时缓存。

    面板只绑回环，经 SSH 隧道进来的请求源全是 127.0.0.1，按 IP 封禁会误伤自己，
    因此用「全局连续失败计数 + 指数退避」对抗在线猜测，并把一次性校验结果缓存 60 秒，
    避免浏览器每个请求都付一次 scrypt 的 CPU 成本。
    """

    def __init__(
        self,
        verifier,
        failure_limit=AUTH_FAILURE_LIMIT,
        lock_base=AUTH_LOCK_BASE_SECONDS,
        lock_max=AUTH_LOCK_MAX_SECONDS,
        cache_seconds=AUTH_CACHE_SECONDS,
        max_parallel=AUTH_MAX_PARALLEL,
    ):
        self.verifier = verifier
        self.failure_limit = failure_limit
        self.lock_base = lock_base
        self.lock_max = lock_max
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until = 0.0
        self._cache = {}
        self._slots = threading.BoundedSemaphore(max_parallel)

    def invalidate(self):
        with self._lock:
            self._cache.clear()

    def _cache_key(self, authorization):
        # 把凭据文件的 mtime 算进 key：重置密码后旧口令的缓存立即失效
        try:
            stamp = self.verifier.path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        return hashlib.sha256(
            f"{stamp}|{authorization}".encode("utf-8", "replace")
        ).hexdigest()

    def check(self, authorization):
        """返回 (是否放行, 建议 Retry-After 秒, 原因)。"""
        now = time.monotonic()
        with self._lock:
            if self._locked_until > now:
                return False, int(self._locked_until - now) + 1, "locked"
            if authorization:
                expiry = self._cache.get(self._cache_key(authorization))
                if expiry and expiry > now:
                    return True, 0, "cached"

        if not self._slots.acquire(blocking=False):
            # 并发校验过多：直接拒绝，避免把 2 核打满（同时压住撞库速率）
            return False, 5, "busy"
        try:
            ok = self.verifier.verify(authorization)
        finally:
            self._slots.release()

        with self._lock:
            if ok:
                self._failures = 0
                self._locked_until = 0.0
                if len(self._cache) >= AUTH_CACHE_MAX:
                    self._cache.clear()
                self._cache[self._cache_key(authorization)] = (
                    time.monotonic() + self.cache_seconds
                )
                return True, 0, "ok"
            self._failures += 1
            count = self._failures
            if count >= self.failure_limit:
                over = count - self.failure_limit
                lock = min(self.lock_base * (2 ** min(over, 6)), self.lock_max)
                self._locked_until = time.monotonic() + lock
                sys.stderr.write(
                    "panel auth: %d 次连续失败，锁定 %d 秒\n" % (count, lock)
                )
                return False, int(lock), "lockout"
            sys.stderr.write("panel auth: 第 %d 次连续失败\n" % count)
            return False, 0, "denied"


class InternalTokenVerifier:
    def __init__(self, path):
        self.expected = Path(path).read_text(encoding="utf-8").strip().encode("utf-8")
        if len(self.expected) < 32:
            raise ValueError("invalid internal token file")

    def verify(self, token):
        if not token:
            return False
        actual = token.strip().encode("utf-8")
        return len(actual) <= 256 and hmac.compare_digest(actual, self.expected)


def run_agent(command, *arguments):
    result = subprocess.run(
        [sys.executable, str(AGENT_FILE), command, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "collector failed")
    return json.loads(result.stdout)


class Handler(BaseHTTPRequestHandler):
    server_version = "BotManager"
    sys_version = ""

    def send_body(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_body(status, body, "application/json; charset=utf-8", extra_headers)

    def request_authentication(self):
        body = b'{"ok":false,"error":"Authentication required."}'
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate",
            'Basic realm="Bot Manager (username: admin)", charset="UTF-8"',
        )
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host", "").partition(":")[0].lower() in {
            "127.0.0.1",
            "localhost",
        }

    def authenticated(self):
        """放行时返回 True；失败时已经把 401/429 写回客户端，调用方直接 return。"""
        ok, retry_after, reason = self.server.auth.check(
            self.headers.get("Authorization")
        )
        if ok:
            return True
        if reason in ("locked", "lockout"):
            self.send_json(
                429,
                {
                    "ok": False,
                    "error": "尝试次数过多，请稍后再试。",
                    "retryAfter": retry_after,
                },
                {"Retry-After": str(max(1, retry_after))},
            )
            return False
        if reason == "busy":
            self.send_json(
                429,
                {"ok": False, "error": "面板正在处理其他请求，请稍后重试。"},
                {"Retry-After": "5"},
            )
            return False
        self.request_authentication()
        return False

    def do_GET(self):
        if not self.valid_host():
            self.send_json(400, {"ok": False, "error": "Invalid host."})
            return

        request = urlsplit(self.path)
        if request.path == "/api/internal/search-usage":
            if not self.server.internal_token.verify(
                self.headers.get("X-Bot-Manager-Token")
            ):
                self.send_json(403, {"ok": False, "error": "Forbidden."})
                return
            try:
                query = parse_qs(request.query)
                since = int(query.get("since", ["0"])[0])
                until = int(query.get("until", [str(int(time.time()))])[0])
                if not 0 <= since <= until <= int(time.time()) + 60:
                    raise ValueError("Invalid time range.")
                # 由持有数据库的服务查询，插件无需创建 SQLite WAL 共享文件。
                usage = self.server.metrics.search_usage(since, until)
                self.send_json(200, {"ok": True, "search": usage["search"], "fetch": usage["fetch"]})
            except (ValueError, OverflowError):
                self.send_json(400, {"ok": False, "error": "Invalid time range."})
            except Exception:
                self.send_json(502, {"ok": False, "error": "Unable to query search usage."})
            return
        if request.path == "/api/internal/deepseek-balance":
            if not self.server.internal_token.verify(
                self.headers.get("X-Bot-Manager-Token")
            ):
                self.send_json(403, {"ok": False, "error": "Forbidden."})
                return
            try:
                payload = self.server.balances.get(force=True)
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "timestamp": payload.get("timestamp"),
                        "deepseek": payload.get("deepseek"),
                    },
                )
            except (OSError, ValueError, RuntimeError) as exc:
                self.send_json(
                    502,
                    {
                        "ok": False,
                        "error": "Unable to query DeepSeek balance.",
                        "detail": str(exc)[:200],
                    },
                )
            return
        if request.path == "/api/internal/credentials":
            if not self.server.internal_token.verify(
                self.headers.get("X-Bot-Manager-Token")
            ):
                self.send_json(403, {"ok": False, "error": "Forbidden."})
                return
            try:
                summary = self.server.balances.config_summary()
            except (OSError, ValueError) as exc:
                summary = {"error": str(exc)[:200]}
            self.send_json(
                200,
                {
                    "ok": True,
                    "panel": self.server.credentials.describe(),
                    "provider": {
                        "deepseek": (summary or {}).get("deepseek") or {},
                        "configured": bool(
                            ((summary or {}).get("deepseek") or {}).get("configured")
                        ),
                    },
                },
            )
            return
        if request.path != "/api/health" and not self.authenticated():
            return
        try:
            if request.path in {"/", "/index.html"}:
                self.send_body(200, PANEL_FILE.read_bytes(), "text/html; charset=utf-8")
            elif request.path == "/api/health":
                self.send_json(200, {"ok": True})
            elif request.path == "/api/config":
                self.send_json(
                    200,
                    {
                        "urls": {
                            "astrbot": "http://127.0.0.1:16185/",
                            "snowluma": "http://127.0.0.1:15099/",
                            "novnc": (
                                "http://127.0.0.1:16081/vnc.html"
                                "?autoconnect=1&resize=scale"
                            ),
                        },
                        "refreshSeconds": 10,
                        # 运维写在 /etc/bot-manager/tunnel-target 里；缺失时前端保留占位符
                        "tunnel": self.server.tunnel_target,
                    },
                )
            elif request.path == "/api/status":
                self.send_json(200, run_agent("status"))
            elif request.path == "/api/metrics":
                query = parse_qs(request.query)
                range_name = query.get("range", ["24h"])[0]
                app_id = query.get("app", ["server"])[0]
                try:
                    payload = self.server.metrics.query(range_name, app_id)
                except ValueError:
                    self.send_json(400, {"ok": False, "error": "Unknown metrics range or application."})
                    return
                self.send_json(200, payload)
            elif request.path == "/api/balances":
                force = parse_qs(request.query).get("refresh", ["0"])[0] == "1"
                self.send_json(200, self.server.balances.get(force=force))
            elif request.path == "/api/provider-config":
                reveal = parse_qs(request.query).get("reveal", ["0"])[0] == "1"
                if reveal and self.headers.get("X-Bot-Manager-Request") != "provider-config-reveal":
                    self.send_json(403, {"ok": False, "error": "Reveal confirmation is missing."})
                    return
                self.send_json(200, self.server.balances.config_summary(reveal=reveal))
            elif request.path == "/api/usage-report":
                query = {
                    key: values[0]
                    for key, values in parse_qs(request.query).items()
                    if values
                }
                self.send_json(200, self.server.usage.report(query))
            elif request.path == "/api/usage-pricing":
                self.send_json(200, self.server.usage.pricing())
            elif request.path == "/api/usage-models":
                self.send_json(200, self.server.usage.available_models())
            elif request.path == "/api/usage-export.csv":
                query = {
                    key: values[0]
                    for key, values in parse_qs(request.query).items()
                    if values
                }
                body = self.server.usage.export_csv(query)
                self.send_body(
                    200,
                    body,
                    "text/csv; charset=utf-8",
                    {
                        "Content-Disposition": (
                            'attachment; filename="bot-manager-usage-report.csv"'
                        )
                    },
                )
            elif request.path == "/api/logs":
                query = parse_qs(request.query)
                service = query.get("service", [""])[0]
                order = query.get("order", ["asc"])[0]
                if service not in ALLOWED_LOG_SERVICES:
                    self.send_json(400, {"ok": False, "error": "Unknown service."})
                    return
                if order not in {"asc", "desc"}:
                    self.send_json(400, {"ok": False, "error": "Unknown log order."})
                    return
                self.send_json(
                    200,
                    run_agent(
                        "logs",
                        "--service",
                        service,
                        "--lines",
                        "240",
                        "--order",
                        order,
                    ),
                )
            else:
                self.send_json(404, {"ok": False, "error": "Not found."})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.send_json(
                502,
                {
                    "ok": False,
                    "error": "Unable to collect server data.",
                    "detail": str(exc)[:300],
                },
            )

    def handle_search_event(self):
        """插件上报一次成功的 DeepSeek Search / Fetch，按次计费在这里落库。"""
        if not self.server.internal_token.verify(
            self.headers.get("X-Bot-Manager-Token")
        ):
            self.send_json(403, {"ok": False, "error": "Forbidden."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 4096:
                raise ValueError("payload size out of range")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            events = payload.get("events")
            if events is None:
                events = [payload]
            if not isinstance(events, list) or not 0 < len(events) <= 50:
                raise ValueError("events must be a list of 1-50 items")
            accepted = 0
            now = int(time.time())
            for item in events:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip()
                if kind not in {"search", "fetch"}:
                    continue
                try:
                    moment = int(item.get("at") or now)
                except (TypeError, ValueError):
                    moment = now
                # 只接受最近两小时内的补报，避免脏数据污染历史区间
                if not now - 7200 <= moment <= now + 60:
                    moment = now
                self.server.metrics.record_search(kind, moment)
                accepted += 1
        except (ValueError, OSError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)[:200]})
            return
        self.send_json(200, {"ok": True, "accepted": accepted})

    def read_internal_payload(self, limit=8192):
        """内部接口的公共前半段：令牌校验 + JSON 体积限制，失败时已自行回包。"""
        if not self.server.internal_token.verify(
            self.headers.get("X-Bot-Manager-Token")
        ):
            self.send_json(403, {"ok": False, "error": "Forbidden."})
            return None
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"ok": False, "error": "JSON content is required."})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > limit:
                raise ValueError("payload size out of range")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)[:200]})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"ok": False, "error": "payload must be an object"})
            return None
        return payload

    def handle_internal_provider_credentials(self):
        """插件把配置里的余额服务凭据下发给面板，由面板负责落盘与采样。"""
        payload = self.read_internal_payload()
        if payload is None:
            return
        update = payload.get("provider", payload)
        if not isinstance(update, dict):
            self.send_json(400, {"ok": False, "error": "provider must be an object"})
            return
        try:
            summary = self.server.balances.update_config(update)
        except (MONITOR.ProviderConfigError, ValueError, OSError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)[:200]})
            return
        self.send_json(
            200,
            {
                "ok": True,
                "deepseek": summary.get("deepseek") or {},
                "siliconflow": summary.get("siliconflow") or {},
            },
        )

    def handle_internal_panel_credentials(self):
        """插件把面板登录账号口令下发过来，这里重算 scrypt 摘要并原子替换。"""
        payload = self.read_internal_payload()
        if payload is None:
            return
        try:
            result = write_panel_credentials(
                self.server.credentials_path,
                payload.get("username"),
                payload.get("password"),
            )
        except (OSError, ValueError, TypeError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)[:200]})
            return
        # 立刻按新文件重载，无需重启面板；同时让旧口令的登录缓存失效。
        self.server.credentials.refresh()
        self.server.auth.invalidate()
        self.send_json(200, {"ok": True, **result})

    def do_POST(self):
        if not self.valid_host():
            self.send_json(400, {"ok": False, "error": "Invalid host."})
            return
        request = urlsplit(self.path)
        if request.path == "/api/internal/search-event":
            self.handle_search_event()
            return
        if request.path == "/api/internal/provider-credentials":
            self.handle_internal_provider_credentials()
            return
        if request.path == "/api/internal/panel-credentials":
            self.handle_internal_panel_credentials()
            return
        if not self.authenticated():
            return
        if request.path not in {
            "/api/provider-config",
            "/api/usage-pricing",
        }:
            self.send_json(404, {"ok": False, "error": "Not found."})
            return
        confirmation = {
            "/api/provider-config": "provider-config",
            "/api/usage-pricing": "usage-pricing",
        }[request.path]
        if self.headers.get("X-Bot-Manager-Request") != confirmation:
            self.send_json(403, {"ok": False, "error": "Request confirmation is missing."})
            return

        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"ok": False, "error": "JSON content is required."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 32768:
                raise MONITOR.ProviderConfigError(
                    "configuration payload must be between 2 and 32768 bytes"
                )
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if request.path == "/api/provider-config":
                summary = self.server.balances.update_config(payload)
            else:
                summary = self.server.usage.update_pricing(payload)
            self.send_json(200, summary)
        except (
            MONITOR.ProviderConfigError,
            MONITOR.UsageConfigError,
            ValueError,
        ) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except (OSError, UnicodeDecodeError) as exc:
            self.send_json(
                500,
                {
                    "ok": False,
                    "error": "Unable to save configuration.",
                    "detail": str(exc)[:200],
                },
            )

    def log_message(self, message, *args):
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), message % args)
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17321)
    parser.add_argument(
        "--credentials",
        default="/etc/bot-manager/credentials.json",
    )
    parser.add_argument(
        "--provider-credentials",
        default="/etc/bot-manager/provider-credentials.json",
    )
    parser.add_argument(
        "--internal-token",
        default="/etc/bot-manager/plugin-token",
    )
    parser.add_argument(
        "--metrics-db",
        default="/var/lib/bot-manager/metrics.db",
    )
    parser.add_argument(
        "--usage-db",
        default="/var/lib/astrbot/data/plugin_usage.db",
    )
    parser.add_argument(
        "--astrbot-db",
        default="/var/lib/astrbot/data/data_v4.db",
    )
    parser.add_argument(
        "--usage-pricing",
        default="/etc/bot-manager/model-pricing.json",
    )
    parser.add_argument(
        "--tunnel-target",
        default="/etc/bot-manager/tunnel-target",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        parser.error("the panel may only bind to a loopback address")

    tunnel_target = ""
    try:
        tunnel_target = Path(args.tunnel_target).read_text(encoding="utf-8").strip()[:120]
    except OSError:
        tunnel_target = ""

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.tunnel_target = tunnel_target
    server.credentials_path = Path(args.credentials)
    server.credentials = CredentialVerifier(args.credentials)
    server.auth = AuthGuard(server.credentials)
    server.internal_token = InternalTokenVerifier(args.internal_token)
    server.metrics = MONITOR.MetricsStore(args.metrics_db)
    server.balances = MONITOR.BalanceService(
        args.provider_credentials,
        cache_seconds=60,
        snapshot_store=server.metrics,
    )
    server.balance_sampler = MONITOR.BalanceSampler(server.balances, 60)
    server.embedding_sampler = MONITOR.EmbeddingUsageSampler(
        args.usage_db,
        "/var/lib/astrbot/data/plugin_data/astrbot_plugin_livingmemory/embedding_cache.db",
    )
    server.usage = MONITOR.UsageReportService(
        args.usage_db,
        args.astrbot_db,
        args.usage_pricing,
        metrics_db=args.metrics_db,
    )
    # 趋势图沿用 60 秒采样；用进程内探针，不再每次跑一遍 docker 状态采集
    server.probe = MONITOR.SystemProbe(
        known_applications=server.metrics.query("1h")["applications"])
    server.sampler = MONITOR.ResourceSampler(
        server.metrics,
        server.probe.collect,
        interval_seconds=60,
    )
    server.daemon_threads = True
    server.sampler.start()
    server.balance_sampler.start()
    server.embedding_sampler.start()
    try:
        server.serve_forever()
    finally:
        server.sampler.stop()
        server.balance_sampler.stop()
        server.embedding_sampler.stop()
        server.server_close()


if __name__ == "__main__":
    main()

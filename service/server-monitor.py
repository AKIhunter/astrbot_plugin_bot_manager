#!/usr/bin/env python3
"""Persistent resource samples and provider balance queries."""

import base64
import hashlib
import hmac
import ipaddress
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import csv
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo


RANGES = {
    # 采样 60 秒一档，短窗口直接给原始点；12 小时以上按小时聚合，只为趋势不放大响应体。
    "1h": {"seconds": 60 * 60, "bucket": 60},
    "6h": {"seconds": 6 * 60 * 60, "bucket": 60},
    "12h": {"seconds": 12 * 60 * 60, "bucket": 3600},
    "24h": {"seconds": 24 * 60 * 60, "bucket": 3600},
    "7d": {"seconds": 7 * 24 * 60 * 60, "bucket": 3600},
}
MAX_CUSTOM_PROVIDERS = 8
MAX_PRICE_ENTRIES = 100
MAX_RESPONSE_BYTES = 1024 * 1024
HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
JSON_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+|\.[0-9]+)*$")
FORBIDDEN_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "transfer-encoding",
}


class ProviderConfigError(ValueError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            "redirects are not allowed for custom balance APIs",
            headers,
            file_pointer,
        )


class MetricsStore:
    RETENTION_SECONDS = 8 * 24 * 60 * 60
    PRUNE_INTERVAL_SECONDS = 6 * 60 * 60

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_prune = 0
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resource_samples (
                    timestamp INTEGER PRIMARY KEY,
                    cpu_percent REAL,
                    memory_percent REAL,
                    disk_percent REAL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS balance_samples (
                    timestamp INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total TEXT NOT NULL,
                    granted TEXT NOT NULL,
                    topped_up TEXT NOT NULL,
                    PRIMARY KEY (timestamp, provider, currency)
                )
                """
            )
            # 联网搜索按次计费：DeepSeek Search / Fetch 自 2026-11-01 起
            # 每成功一次 0.005 元，且完全不消耗 token，只能在这里单独记流水。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    kind TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_events_timestamp "
                "ON search_events (timestamp)"
            )

    @staticmethod
    def _percent(used, total):
        try:
            return round(float(used) / float(total) * 100, 2) if float(total) > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def record(self, status):
        server = status.get("server") or {}
        timestamp = int(status.get("timestamp") or time.time())
        memory = server.get("memory") or {}
        disk = server.get("disk") or {}
        values = (
            timestamp,
            server.get("cpuPercent"),
            self._percent(memory.get("used"), memory.get("total")),
            self._percent(disk.get("used"), disk.get("total")),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO resource_samples
                    (timestamp, cpu_percent, memory_percent, disk_percent)
                VALUES (?, ?, ?, ?)
                """,
                values,
            )
            # 10 秒一条的采集频率下，不必每次都做过期清理
            if timestamp - self._last_prune >= self.PRUNE_INTERVAL_SECONDS:
                self._last_prune = timestamp
                connection.execute(
                    "DELETE FROM resource_samples WHERE timestamp < ?",
                    (timestamp - self.RETENTION_SECONDS,),
                )

    def record_search(self, kind, timestamp=None):
        """记一条搜索/抓取流水。kind 只允许 search / fetch。"""
        if kind not in {"search", "fetch"}:
            raise ValueError("unsupported search kind")
        moment = int(timestamp or time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO search_events (timestamp, kind) VALUES (?, ?)",
                (moment, kind),
            )

    def search_usage(self, since, until=None):
        """按类型汇总 [since, until) 内的次数，并给出按小时的分布。"""
        end = int(until or time.time())
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT kind, COUNT(*) FROM search_events
                WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY kind
                """,
                (int(since), end),
            ).fetchall()
            hourly = connection.execute(
                """
                SELECT CAST(timestamp / 3600 AS INTEGER) * 3600 AS bucket, COUNT(*)
                FROM search_events
                WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY bucket
                """,
                (int(since), end),
            ).fetchall()
        counts = {"search": 0, "fetch": 0}
        for kind, value in rows:
            if kind in counts:
                counts[kind] = int(value)
        return {
            "search": counts["search"],
            "fetch": counts["fetch"],
            "total": counts["search"] + counts["fetch"],
            "byHour": {int(bucket): int(value) for bucket, value in hourly},
        }

    def query(self, range_name):
        settings = RANGES.get(range_name)
        if not settings:
            raise ValueError("unsupported metrics range")
        now = int(time.time())
        cutoff = now - settings["seconds"]
        bucket = settings["bucket"]
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    CAST(timestamp / ? AS INTEGER) * ? AS bucket_timestamp,
                    AVG(cpu_percent),
                    AVG(memory_percent),
                    AVG(disk_percent),
                    COUNT(*)
                FROM resource_samples
                WHERE timestamp >= ?
                GROUP BY bucket_timestamp
                ORDER BY bucket_timestamp
                """,
                (bucket, bucket, cutoff),
            ).fetchall()
        return {
            "ok": True,
            "range": range_name,
            "bucketSeconds": bucket,
            "from": cutoff,
            "to": now,
            "samples": [
                {
                    "timestamp": row[0],
                    "cpuPercent": round(row[1], 2) if row[1] is not None else None,
                    "memoryPercent": round(row[2], 2) if row[2] is not None else None,
                    "diskPercent": round(row[3], 2) if row[3] is not None else None,
                    "sampleCount": row[4],
                }
                for row in rows
            ],
        }

    def record_balances(self, provider, balances, timestamp):
        rows = []
        for item in balances or []:
            currency = str(item.get("currency") or "").strip()
            try:
                total = Decimal(str(item.get("total") or ""))
                granted = Decimal(str(item.get("granted") or "0"))
                topped_up = Decimal(str(item.get("toppedUp") or "0"))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not currency or not all(
                value.is_finite() for value in (total, granted, topped_up)
            ):
                continue
            rows.append(
                (
                    int(timestamp),
                    provider,
                    currency,
                    format(total, "f"),
                    format(granted, "f"),
                    format(topped_up, "f"),
                )
            )
        if not rows:
            return
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO balance_samples
                    (timestamp, provider, currency, total, granted, topped_up)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                "DELETE FROM balance_samples WHERE timestamp < ?",
                (int(timestamp) - 90 * 24 * 60 * 60,),
            )

    def balance_observation(self, provider, currency, since):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT timestamp, total, granted, topped_up
                FROM balance_samples
                WHERE provider = ? AND currency = ? AND timestamp >= ?
                ORDER BY timestamp
                """,
                (provider, currency, int(since)),
            ).fetchall()
        if not rows:
            return None
        first, last = rows[0], rows[-1]
        net_decrease = Decimal(first[1]) - Decimal(last[1])
        return {
            "from": first[0],
            "to": last[0],
            "firstTotal": first[1],
            "lastTotal": last[1],
            "netDecrease": format(
                net_decrease.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
                "f",
            ),
            "sampleCount": len(rows),
            "isFullDay": first[0] <= int(since) + 10 * 60,
        }


class SystemProbe:
    """进程内极轻量资源探针：只读 /proc 与 statvfs，不启子进程、不查 docker。

    10 秒一次采集必须便宜：`run_agent("status")` 每次都要跑 docker ps/stats，
    那是给概览卡片用的，不能拿来喂趋势图。
    """

    def __init__(self):
        self._previous = self._cpu_stat()

    @staticmethod
    def _cpu_stat():
        try:
            with open("/proc/stat", "r", encoding="utf-8") as handle:
                fields = [int(value) for value in handle.readline().split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    def cpu_percent(self):
        current = self._cpu_stat()
        previous, self._previous = self._previous, current
        if current is None or previous is None:
            return None
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        if total <= 0:
            return None
        return round(100 * (1 - idle / total), 1)

    @staticmethod
    def _memory():
        values = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.replace(":", "").split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        values[parts[0]] = int(parts[1]) * 1024
        except OSError:
            return {"total": 0, "used": 0}
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        return {"total": total, "used": max(0, total - available)}

    def collect(self):
        cpu = self.cpu_percent()
        if cpu is None:  # 首次采样没有上一次快照，补一次短间隔读数
            time.sleep(0.1)
            cpu = self.cpu_percent()
        try:
            disk = shutil.disk_usage("/")
            disk_info = {"total": disk.total, "used": disk.used}
        except OSError:
            disk_info = {"total": 0, "used": 0}
        return {
            "timestamp": int(time.time()),
            "server": {
                "cpuPercent": cpu,
                "memory": self._memory(),
                "disk": disk_info,
            },
        }


class ResourceSampler:
    def __init__(self, store, collect, interval_seconds=60):
        self.store = store
        self.collect = collect
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="resource-sampler",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.store.record(self.collect())
            except Exception as exc:  # Keep monitoring independent from the panel.
                print(f"resource sampler failed: {str(exc)[:200]}", flush=True)
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(1, self.interval_seconds - elapsed))


class BalanceSampler:
    """独立线程定时刷新余额，为小时级消费折算提供足够的采样密度。"""

    def __init__(self, service, interval_seconds=60):
        self.service = service
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="balance-sampler",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.service.get(force=True)
            except Exception as exc:  # 余额接口异常不应拖垮面板
                print(f"balance sampler failed: {str(exc)[:200]}", flush=True)
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(1, self.interval_seconds - elapsed))


class EmbeddingUsageSampler(ResourceSampler):
    """以数据库所属用户导入已登记的网关累计用量，无需重启 AstrBot。"""

    def __init__(self, usage_db, cache_db, interval_seconds=15):
        self.usage_db = str(usage_db)
        self.cache_db = str(cache_db)
        super().__init__(None, None, interval_seconds)
        self.thread.name = "embedding-usage-sampler"

    def _run(self):
        helper = Path(__file__).with_name("server-embedding-usage.py")
        last_error = None
        while not self.stop_event.is_set():
            try:
                command = [shutil.which("python3") or "python3", str(helper),
                           "--usage-db", self.usage_db, "--cache-db", self.cache_db]
                if os.geteuid() == 0:
                    command = ["/usr/sbin/runuser", "-u", "astrbot", "--", *command]
                result = subprocess.run(command, capture_output=True, text=True, timeout=12)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip().splitlines()[-1][:200])
                health = json.loads(result.stdout).get("health", {})
                pending_stale = (health.get("oldestPendingAt") and
                                 time.time() - health["oldestPendingAt"] > 120)
                if health.get("unknownUpstreamRequests") or health.get("counterMismatch") or pending_stale:
                    raise RuntimeError(
                        f"journal usage needs attention: unknown={health.get('unknownUpstreamRequests', 0)}, "
                        f"counterMismatch={health.get('counterMismatch', False)}"
                    )
                last_error = None
            except Exception as exc:
                # 同一异常只报一次，跨日/计数回退等待人工核对，避免累计重复入账。
                error = str(exc)[:200]
                if error != last_error:
                    print(f"embedding usage sampler failed: {error}", flush=True)
                last_error = error
            self.stop_event.wait(self.interval_seconds)


class UsageConfigError(ValueError):
    pass


class UsageReportService:
    REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
    RANGE_SECONDS = {
        "24h": 24 * 60 * 60,
        "7d": 7 * 24 * 60 * 60,
        "30d": 30 * 24 * 60 * 60,
    }
    PRICE_FIELDS = (
        "inputOtherCnyPerMillion",
        "inputCachedCnyPerMillion",
        "outputCnyPerMillion",
    )
    SCHEDULE_PRICE_FIELDS = (
        "inputOtherOffPeakPerMillion",
        "inputCachedOffPeakPerMillion",
        "outputOffPeakPerMillion",
        "inputOtherPeakPerMillion",
        "inputCachedPeakPerMillion",
        "outputPeakPerMillion",
    )

    def __init__(self, telemetry_db, astrbot_db, pricing_path, metrics_db=None,
                 astrbot_config=None):
        self.telemetry_db = Path(telemetry_db)
        self.astrbot_db = Path(astrbot_db)
        self.pricing_path = Path(pricing_path)
        self.metrics_db = Path(metrics_db) if metrics_db else None
        self.lock = threading.Lock()
        self._search_store = None
        self.astrbot_config = Path(astrbot_config or self.astrbot_db.parent / "cmd_config.json")
        self._model_cache = None
        self._model_cache_at = 0
        self._model_lock = threading.Lock()

    @staticmethod
    def _provider_usage_models(config):
        """只返回模型与类型，不向页面传递 API Key 等连接配置。"""
        if not isinstance(config, dict):
            return []
        sources = {p.get("id"): p for p in (config.get("provider_sources") or [])
                   if isinstance(p, dict)}
        models = []
        for provider in config.get("provider") or []:
            if not isinstance(provider, dict) or provider.get("enable", True) is False:
                continue
            source = sources.get(provider.get("provider_source_id"), {})
            provider_type = str(provider.get("type") or source.get("type") or "").lower()
            model = next((provider.get(key) for key in (
                "embedding_model", "rerank_model", "reranking_model", "model",
                "tts_model", "stt_model",
            ) if provider.get(key)), None)
            if not isinstance(model, str) or not model.strip() or model == "automatic":
                continue
            if provider.get("embedding_model") or "embedding" in provider_type:
                kind = "embedding"
            elif provider.get("rerank_model") or provider.get("reranking_model") or "rerank" in provider_type:
                kind = "rerank"
            elif "tts" in provider_type or "text_to_speech" in provider_type:
                kind = "tts"
            elif "stt" in provider_type or "speech_to_text" in provider_type:
                kind = "stt"
            else:
                kind = "llm"
            models.append({
                "model": model.strip(), "providerId": str(provider.get("id") or ""),
                "name": model.strip(), "kind": kind, "source": "configured",
            })
        return models

    def available_models(self):
        """直连及各类 Provider 加上 automatic 健康模型；配置存在不代表连通已验证。"""
        with self._model_lock:
            if self._model_cache and time.monotonic() - self._model_cache_at < 10:
                return self._model_cache
            config = {}
            try:
                config = json.loads(self.astrbot_config.read_text(encoding="utf-8-sig"))
                if not isinstance(config, dict):
                    config = {}
            except (OSError, ValueError, TypeError):
                pass
            configured = self._provider_usage_models(config)
            try:
                dashboard = config["dashboard"]
                encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=")
                header = encode(b'{"alg":"HS256","typ":"JWT"}')
                claims = encode(json.dumps({
                    "username": dashboard["username"], "exp": int(time.time()) + 30,
                }).encode())
                message = header + b"." + claims
                signature = encode(hmac.new(
                    dashboard["jwt_secret"].encode(), message, hashlib.sha256,
                ).digest())
                # 管理员凭据只在服务端内存使用，固定访问回环地址且不跟随重定向。
                request = urllib.request.Request(
                    "http://127.0.0.1:6185/api/plug/astrbot_plugin_multi_api_router/upstreams",
                    headers={"Authorization": "Bearer " + (message + b"." + signature).decode()},
                )
                with urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirectHandler(),
                ).open(request, timeout=3) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("router response too large")
                payload = json.loads(raw)
                data = payload.get("data") or payload
                upstreams = data.get("upstreams") or []
                models = []
                for item in upstreams:
                    health = item.get("health") or {}
                    if (data.get("runtime_upstream_count", 0) > 0
                            and item.get("enabled") and item.get("configured")
                            and not item.get("checking") and health.get("ok") is True
                            and item.get("model")):
                        models.append({
                            "model": item["model"],
                            "providerId": data.get("provider_id") or "multi_api_router",
                            "name": item.get("name") or item["model"],
                            "host": item.get("host") or "",
                            "slot": item.get("slot"),
                            "checkedAt": health.get("checked_at"),
                            "kind": "llm", "source": "router",
                        })
                result = {
                    "ok": True, "group": "automatic", "models": models,
                    "excludedCount": len(upstreams) - len(models),
                }
            except (OSError, ValueError, KeyError, TypeError):
                # 读取失败时不沿用旧的正常列表，以免异常模型继续进入选择框。
                result = {"ok": False, "group": "automatic", "models": [],
                          "error": "无法读取路由健康状态，请检查 AstrBot 与路由插件。"}
            # 路由不可用时，仍可登记已配置的向量、语音等模型单价和额度。
            result["routerModelCount"] = len(result["models"])
            result["configuredModelCount"] = len(configured)
            seen = {(p["providerId"], p["model"]) for p in result["models"]}
            for item in configured:
                key = (item["providerId"], item["model"])
                if key not in seen:
                    result["models"].append(item)
                    seen.add(key)
            self._model_cache = result
            self._model_cache_at = time.monotonic()
            return result

    # ------------------------------------------------------ 联网搜索按次计费
    SEARCH_UNIT_PRICE_CNY = Decimal("0.005")

    def _search_summary(self, since, until):
        """DeepSeek Search / Fetch 自 2026-11-01 起每次成功 0.005 元，不消耗 token。"""
        if not self.metrics_db or not self.metrics_db.exists():
            return {
                "available": False,
                "searchCount": 0,
                "fetchCount": 0,
                "total": 0,
                "unitPriceCny": format(self.SEARCH_UNIT_PRICE_CNY, "f"),
                "amountCny": "0.000000",
                "byHour": {},
            }
        try:
            if self._search_store is None:
                self._search_store = MetricsStore(self.metrics_db)
            usage = self._search_store.search_usage(since, until)
        except (OSError, ValueError, sqlite3.Error):
            return {
                "available": False,
                "searchCount": 0,
                "fetchCount": 0,
                "total": 0,
                "unitPriceCny": format(self.SEARCH_UNIT_PRICE_CNY, "f"),
                "amountCny": "0.000000",
                "byHour": {},
            }
        amount = (Decimal(usage["total"]) * self.SEARCH_UNIT_PRICE_CNY).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        return {
            "available": True,
            "searchCount": usage["search"],
            "fetchCount": usage["fetch"],
            "total": usage["total"],
            "unitPriceCny": format(self.SEARCH_UNIT_PRICE_CNY, "f"),
            "amountCny": format(amount, "f"),
            "byHour": {int(key): int(value) for key, value in (usage.get("byHour") or {}).items()},
        }

    def _balance_samples(self, since):
        """读取指定时间之后的 DeepSeek 余额快照，返回 [(时间戳, 余额)]。"""
        if not self.metrics_db or not self.metrics_db.exists():
            return []
        try:
            with closing(self._read_only(self.metrics_db)) as connection:
                rows = connection.execute(
                    """
                    SELECT timestamp, total FROM balance_samples
                    WHERE provider = 'deepseek' AND timestamp >= ?
                    ORDER BY timestamp
                    """,
                    (int(since),),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        samples = []
        for row in rows:
            try:
                samples.append((float(row["timestamp"]), float(row["total"])))
            except (TypeError, ValueError):
                continue
        return samples

    @staticmethod
    def _balance_at(samples, moment):
        """线性插值得到 moment 时刻的余额；采样覆盖不足时返回 None。"""
        if not samples:
            return None
        if moment < samples[0][0]:
            return samples[0][1] if samples[0][0] - moment <= 600 else None
        if moment >= samples[-1][0]:
            return samples[-1][1] if moment - samples[-1][0] <= 900 else None
        low, high = 0, len(samples) - 1
        while low + 1 < high:
            middle = (low + high) // 2
            if samples[middle][0] <= moment:
                low = middle
            else:
                high = middle
        start_at, start_value = samples[low]
        end_at, end_value = samples[high]
        if end_at <= start_at:
            return start_value
        ratio = (moment - start_at) / (end_at - start_at)
        return start_value + (end_value - start_value) * ratio

    def _balance_consumption(self, cutoff, now):
        """按小时统计余额差，得到官方口径的消费金额。"""
        summary = {
            "available": False,
            "samples": 0,
            "startAt": None,
            "startTotal": None,
            "endAt": None,
            "endTotal": None,
            "consumed": None,
            "recharge": False,
        }
        if cutoff <= 0 or now - cutoff > 7 * 86400:
            return summary, [], []
        samples = self._balance_samples(cutoff - 1800)
        summary["samples"] = len(samples)
        if len(samples) < 2:
            return summary, [], samples
        start_value = self._balance_at(samples, cutoff)
        end_value = self._balance_at(samples, now)
        if start_value is None or end_value is None:
            return summary, [], samples
        start_value = self._balance_at(samples, cutoff)
        end_value = self._balance_at(samples, now)
        if start_value is None or end_value is None:
            return summary, []
        summary.update(
            {
                "available": True,
                "startAt": cutoff,
                "startTotal": format(start_value, ".2f"),
                "endAt": now,
                "endTotal": format(end_value, ".2f"),
            }
        )
        total = Decimal(str(start_value)) - Decimal(str(end_value))
        summary["consumed"] = format(
            total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"
        )
        summary["recharge"] = total < 0

        hours = []
        cursor = int(cutoff)
        limit = int(now)
        while cursor < limit:
            edge = min(cursor + 3600, limit)
            head = self._balance_at(samples, cursor)
            tail = self._balance_at(samples, edge)
            consumed = None
            if head is not None and tail is not None and edge - cursor >= 600:
                consumed = format(
                    (Decimal(str(head)) - Decimal(str(tail))).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    ),
                    "f",
                )
            hours.append(
                {
                    "key": datetime.fromtimestamp(
                        cursor, self.REPORT_TIMEZONE
                    ).strftime("%Y-%m-%dT%H"),
                    "hour": datetime.fromtimestamp(
                        cursor, self.REPORT_TIMEZONE
                    ).strftime("%H:00"),
                    "startAt": cursor,
                    "endAt": edge,
                    "startBalance": None if head is None else format(head, ".2f"),
                    "endBalance": None if tail is None else format(tail, ".2f"),
                    "consumed": consumed,
                }
            )
            cursor = edge
        points = [(cutoff, start_value)]
        for sample in samples:
            if cutoff < sample[0] < now:
                points.append(sample)
        points.append((now, end_value))
        return summary, hours, points

    def _attribute_balance_to_hours(self, events, samples):
        """把每个采样区间的真实余额降幅，按该区间内各笔调用的估算金额比例分摊到小时。

        返回 (按小时的实测消费, 按小时的无调用记录消费)。
        """
        attributed = {}
        orphaned = {}
        if len(samples) < 2:
            return attributed, orphaned
        pairs = []
        for index in range(len(samples) - 1):
            start_at, start_value = samples[index]
            end_at, end_value = samples[index + 1]
            delta = start_value - end_value
            pairs.append({"from": start_at, "to": end_at, "delta": delta, "items": []})
        if not pairs:
            return attributed, orphaned
        first_at = pairs[0]["from"]
        last_at = pairs[-1]["to"]
        for event in events:
            moment = event["started_at"]
            if moment < first_at or moment >= last_at:
                continue
            weight = float(event["amount"]) if event["amount"] else 0.0
            for pair in pairs:
                if pair["from"] <= moment < pair["to"]:
                    if weight > 0:
                        pair["items"].append((moment, weight))
                    break
        for pair in pairs:
            delta = pair["delta"]
            if abs(delta) < 0.0001:
                continue
            bucket_key = lambda ts: datetime.fromtimestamp(
                ts, self.REPORT_TIMEZONE
            ).strftime("%Y-%m-%dT%H")
            if delta < 0:
                # 余额上升视为充值，不计入消费，也不做分摊
                continue
            if not pair["items"]:
                key = bucket_key((pair["from"] + pair["to"]) / 2)
                orphaned[key] = orphaned.get(key, 0.0) + delta
                continue
            total_weight = sum(item[1] for item in pair["items"])
            if total_weight <= 0:
                key = bucket_key((pair["from"] + pair["to"]) / 2)
                orphaned[key] = orphaned.get(key, 0.0) + delta
                continue
            for moment, weight in pair["items"]:
                key = bucket_key(moment)
                attributed[key] = attributed.get(key, 0.0) + delta * (weight / total_weight)
        return attributed, orphaned

    @staticmethod
    def _read_only(path):
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _text(value, field, maximum, required=False):
        if not isinstance(value, str):
            raise UsageConfigError(f"{field} must be a string")
        value = value.strip()
        if required and not value:
            raise UsageConfigError(f"{field} is required")
        if len(value) > maximum:
            raise UsageConfigError(f"{field} is too long")
        return value

    @staticmethod
    def _timestamp(value, field):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            timestamp = parsed.timestamp()
        except (TypeError, ValueError, OverflowError) as exc:
            raise UsageConfigError(f"{field} must be an ISO 8601 timestamp") from exc
        if timestamp < 0:
            raise UsageConfigError(f"{field} is out of range")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _decimal(value, field, required=False):
        if value in ("", None):
            if required:
                raise UsageConfigError(f"{field} is required")
            return ""
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise UsageConfigError(f"{field} must be a decimal number") from exc
        if not number.is_finite() or number < 0 or number > Decimal("1000000"):
            raise UsageConfigError(f"{field} is out of range")
        return format(number.normalize(), "f")

    def _normalize_prices(self, payload):
        if not isinstance(payload, dict):
            raise UsageConfigError("pricing configuration must be an object")
        if set(payload) - {"version", "prices", "history", "freeQuotas"}:
            raise UsageConfigError("pricing configuration contains unsupported fields")
        prices = payload.get("prices") or []
        if not isinstance(prices, list):
            raise UsageConfigError("prices must be a list")
        if len(prices) > MAX_PRICE_ENTRIES:
            raise UsageConfigError(
                f"no more than {MAX_PRICE_ENTRIES} price entries are allowed"
            )
        normalized = []
        identifiers = set()
        for index, item in enumerate(prices):
            if not isinstance(item, dict):
                raise UsageConfigError(f"price entry {index + 1} must be an object")
            allowed = {
                "id",
                "providerId",
                "model",
                "effectiveFrom",
                "currency",
                "pricingMode",
                "holidayDates",
                "providerIds",
                "modelAliases",
                "inputBatchCnyPerMillion",
                *self.PRICE_FIELDS,
                *self.SCHEDULE_PRICE_FIELDS,
            }
            if set(item) - allowed:
                raise UsageConfigError(
                    f"price entry {index + 1} contains unsupported fields"
                )
            identifier = self._text(
                item.get("id") or f"price-{secrets.token_hex(5)}",
                "id",
                64,
                required=True,
            )
            if not IDENTIFIER_RE.fullmatch(identifier) or identifier in identifiers:
                raise UsageConfigError("price entry IDs must be unique URL-safe names")
            identifiers.add(identifier)
            for field in ("providerIds", "modelAliases"):
                values = item.get(field) or []
                if not isinstance(values, list) or len(values) > 30:
                    raise UsageConfigError(f"{field} must be a list of at most 30 names")
                item[field] = sorted(set(self._text(v, field, 160, True) for v in values))
            pricing_mode = self._text(
                item.get("pricingMode", "flat"),
                "pricingMode",
                32,
                required=True,
            )
            if pricing_mode not in {"flat", "deepseek_peak", "embedding"}:
                raise UsageConfigError("pricingMode is unsupported")
            currency = self._text(
                item.get("currency", "CNY"),
                "currency",
                8,
                required=True,
            ).upper()
            if currency != "CNY":
                raise UsageConfigError("单价必须使用人民币 CNY；历史外币价需先按已核实换算迁移")
            holiday_dates = item.get("holidayDates") or []
            if not isinstance(holiday_dates, list) or len(holiday_dates) > 100:
                raise UsageConfigError("holidayDates must be a list of at most 100 dates")
            normalized_holidays = []
            for holiday in holiday_dates:
                try:
                    normalized_holidays.append(
                        datetime.strptime(str(holiday), "%Y-%m-%d").date().isoformat()
                    )
                except ValueError as exc:
                    raise UsageConfigError(
                        "holidayDates must use YYYY-MM-DD"
                    ) from exc
            normalized.append(
                {
                    "id": identifier,
                    "providerId": self._text(
                        item.get("providerId", ""),
                        "providerId",
                        160,
                    ),
                    "model": self._text(
                        item.get("model", ""),
                        "model",
                        160,
                        required=True,
                    ),
                    "effectiveFrom": self._timestamp(
                        item.get("effectiveFrom", ""),
                        "effectiveFrom",
                    ),
                    "currency": currency,
                    "providerIds": item["providerIds"],
                    "modelAliases": item["modelAliases"],
                    "pricingMode": pricing_mode,
                    "holidayDates": sorted(set(normalized_holidays)),
                    **({
                        "inputBatchCnyPerMillion": self._decimal(
                            item.get("inputBatchCnyPerMillion", ""), "inputBatchCnyPerMillion",
                        ),
                    } if pricing_mode == "embedding" or "inputBatchCnyPerMillion" in item else {}),
                    **{
                        field: self._decimal(item.get(field, ""), field)
                        for field in self.PRICE_FIELDS
                    },
                    **{
                        field: self._decimal(
                            item.get(field, ""),
                            field,
                            required=pricing_mode == "deepseek_peak",
                        )
                        for field in self.SCHEDULE_PRICE_FIELDS
                    },
                }
            )
        normalized.sort(key=lambda item: (item["model"], item["effectiveFrom"]))
        keys = [(p["model"], p["providerId"], tuple(p["providerIds"]), p["effectiveFrom"])
                for p in normalized]
        if len(keys) != len(set(keys)):
            raise UsageConfigError("同一模型、Provider 和生效时间不能重复配置")
        history = payload.get("history") or []
        if not isinstance(history, list):
            raise UsageConfigError("history must be a list")
        normalized_history = (
            self._normalize_prices({"prices": history})["prices"] if history else []
        )
        if identifiers & {p["id"] for p in normalized_history}:
            raise UsageConfigError("当前与历史价格 ID 不能重复")
        quotas = payload.get("freeQuotas") or []
        if not isinstance(quotas, list) or len(quotas) > MAX_PRICE_ENTRIES:
            raise UsageConfigError("freeQuotas must be a bounded list")
        normalized_quotas = []
        quota_keys = set()
        for quota in quotas:
            if not isinstance(quota, dict) or set(quota) - {
                "id", "model", "providerId", "totalTokens", "usedTokensBefore",
                "startsAt", "expiresAt", "note",
            }:
                raise UsageConfigError("免费额度字段不合法")
            model = self._text(quota.get("model", ""), "model", 160, True)
            provider = self._text(quota.get("providerId", ""), "providerId", 160, True)
            key = (provider, model)
            if key in quota_keys:
                raise UsageConfigError("每个 Provider / 模型只能配置一份免费额度")
            quota_keys.add(key)
            counts = {}
            for field in ("totalTokens", "usedTokensBefore"):
                value = quota.get(field, 0)
                if isinstance(value, bool) or not re.fullmatch(r"\d{1,13}", str(value)):
                    raise UsageConfigError(f"{field} 必须为非负整数")
                counts[field] = int(value)
            start = self._timestamp(quota.get("startsAt", ""), "startsAt")
            end = self._timestamp(quota["expiresAt"], "expiresAt") if quota.get("expiresAt") else ""
            if end and end <= start:
                raise UsageConfigError("免费额度到期时间必须晚于开始时间")
            normalized_quotas.append({
                "id": self._text(quota.get("id") or f"quota-{secrets.token_hex(5)}", "id", 64, True),
                "model": model, "providerId": provider, **counts,
                "startsAt": start, "expiresAt": end,
                "note": self._text(quota.get("note", ""), "note", 300),
            })
        return {"version": 2, "prices": normalized, "history": normalized_history,
                "freeQuotas": normalized_quotas}

    def pricing(self):
        with self.lock:
            try:
                payload = json.loads(self.pricing_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                payload = {"version": 1, "prices": []}
            return {
                "ok": True,
                **self._normalize_prices(payload),
                "limits": {"priceEntries": MAX_PRICE_ENTRIES},
            }

    def update_pricing(self, payload):
        with self.lock:
            # 页面只编辑当前价格，保存时服务端保留历史，避免旧费用被改写。
            payload = dict(payload)
            if self.pricing_path.exists():
                stored = json.loads(self.pricing_path.read_text(encoding="utf-8"))
                payload.setdefault("history", stored.get("history", []))
                payload.setdefault("freeQuotas", stored.get("freeQuotas", []))
                payload = self._normalize_prices(payload)
                # 修改为新的生效版本时归档旧价；历史额度与价格独立，不会重置。
                previous = {p["id"]: p for p in stored.get("prices", [])}
                history = list(payload["history"])
                for price in payload.get("prices") or []:
                    old = previous.get(price.get("id"))
                    if old and self._price_timestamp(price) > self._price_timestamp(old):
                        history.append(dict(old, id=f"history-{secrets.token_hex(6)}"))
                payload["history"] = history
            normalized = self._normalize_prices(payload)
            self.pricing_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.pricing_path.with_suffix(
                self.pricing_path.suffix + ".tmp"
            )
            temporary.write_text(
                json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.pricing_path)
            if os.geteuid() == 0:
                os.chown(self.pricing_path, 0, 0)
        return self.pricing()

    def _installed_at(self):
        if not self.telemetry_db.exists():
            return None
        try:
            with closing(self._read_only(self.telemetry_db)) as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'installed_at'"
                ).fetchone()
            if not row:
                raise RuntimeError("telemetry installation boundary is missing")
            return float(row["value"])
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError(
                "telemetry installation boundary is unavailable"
            ) from exc

    @staticmethod
    def _matches(event, filters):
        return all(
            not filters.get(field) or event.get(field) == filters[field]
            for field in ("plugin_id", "model", "status")
        )

    def _telemetry_events(self, cutoff, now, filters):
        if not self.telemetry_db.exists():
            return []
        with closing(self._read_only(self.telemetry_db)) as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(usage_events)")
            }
            # 新旧采集库兼容：旧记录没有 usage_known，不把取消后的零当免费。
            extra = ", " + ", ".join(
                field if field in columns else f"NULL AS {field}"
                for field in ("usage_known", "request_details", "call_kind")
            )
            rows = connection.execute(
                f"""
                SELECT call_id, started_at, ended_at, plugin_id, route, source,
                       provider_id, model, token_input_other,
                       token_input_cached, token_output, status, latency_ms,
                       ttft_ms, error_type, attempts {extra}
                FROM usage_events
                WHERE started_at >= ? AND started_at <= ?
                ORDER BY started_at DESC
                """,
                (cutoff, now),
            ).fetchall()
            has_receipts = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_reconciliations'"
            ).fetchone()
            receipts = {}
            if has_receipts:
                receipts = {row["call_id"]: dict(row) for row in connection.execute(
                    "SELECT r.* FROM usage_reconciliations r JOIN usage_events e "
                    "ON e.call_id=r.call_id WHERE e.started_at>=? AND e.started_at<=?",
                    (cutoff, now))}
            measurements = {}
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='usage_embedding_measurements'"
            ).fetchone():
                measurements = {r["call_id"]: json.loads(r["payload_json"]) for r in
                                connection.execute(
                                    "SELECT m.* FROM usage_embedding_measurements m "
                                    "JOIN usage_events e ON e.call_id=m.call_id "
                                    "WHERE e.started_at>=? AND e.started_at<=?", (cutoff, now))}
        events = []
        for row in rows:
            event = dict(row)
            if event["call_id"] in measurements:
                event.update(measurements[event["call_id"]])
                event["request_details"] = json.dumps(event["request_details"])
            if event["call_id"] in receipts:
                receipt = receipts[event["call_id"]]
                if receipt["period_start"] >= now:
                    continue
                event.update({key: receipt[key] for key in
                              ("record_type", "note", "period_start", "period_end")})
                event["fixed_amount_cny"] = receipt["amount_cny"]
                event["latency_ms"] = None
            event["call_kind"] = event.get("call_kind") or "llm"
            if event["usage_known"] is None:
                event["usage_known"] = any(
                    event[field] for field in
                    ("token_input_other", "token_input_cached", "token_output")
                )
            else:
                event["usage_known"] = bool(event["usage_known"])
            try:
                requests = json.loads(event["request_details"] or "[]")
                event["request_details"] = requests if isinstance(requests, list) else []
            except (TypeError, ValueError):
                event["request_details"] = []
            event["unknown_requests"] = sum(
                not item.get("usage_known", False)
                for item in event["request_details"] if isinstance(item, dict)
            )
            event["historical"] = False
            if self._matches(event, filters):
                events.append(event)
        return events

    def _usage_monitoring(self, now):
        if not self.telemetry_db.exists():
            return None
        with closing(self._read_only(self.telemetry_db)) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='embedding_gateway_health_v1'"
            ).fetchone()
        if not row:
            return None
        health = json.loads(row["value"])
        health["syncLagSeconds"] = max(0, round(now - health.get("lastSyncAt", 0)))
        health["stale"] = health["syncLagSeconds"] > 90
        health["pendingStale"] = bool(health.get("oldestPendingAt") and
                                     now - health["oldestPendingAt"] > 120)
        return health

    def _historical_events(self, cutoff, boundary, filters):
        if not self.astrbot_db.exists():
            return []
        maximum = boundary if boundary is not None else time.time() + 1
        with closing(self._read_only(self.astrbot_db)) as connection:
            rows = connection.execute(
                """
                SELECT id, agent_type, status, provider_id, provider_model,
                       token_input_other, token_input_cached, token_output,
                       start_time, end_time, time_to_first_token
                FROM provider_stats
                WHERE start_time >= ? AND start_time < ?
                ORDER BY start_time DESC
                """,
                (cutoff, maximum),
            ).fetchall()
        events = []
        for row in rows:
            event = {
                "call_id": f"historical:{row['id']}",
                "started_at": float(row["start_time"] or 0),
                "ended_at": float(row["end_time"] or row["start_time"] or 0),
                "plugin_id": "unattributed",
                "route": f"historical/{row['agent_type'] or 'unknown'}",
                "source": "astrbot_provider_stats",
                "provider_id": str(row["provider_id"] or "unknown"),
                "model": str(row["provider_model"] or "unknown"),
                "token_input_other": max(0, int(row["token_input_other"] or 0)),
                "token_input_cached": max(0, int(row["token_input_cached"] or 0)),
                "token_output": max(0, int(row["token_output"] or 0)),
                "status": str(row["status"] or "unknown"),
                "latency_ms": max(
                    0,
                    round(
                        (
                            float(row["end_time"] or 0)
                            - float(row["start_time"] or 0)
                        )
                        * 1000
                    ),
                ),
                "ttft_ms": (
                    max(0, round(float(row["time_to_first_token"]) * 1000))
                    if row["time_to_first_token"] is not None
                    else None
                ),
                "error_type": None,
                "attempts": 1,
                "historical": True,
                "usage_known": any(
                    row[field] for field in
                    ("token_input_other", "token_input_cached", "token_output")
                ),
                "request_details": [],
                "unknown_requests": 0,
                "call_kind": "llm",
            }
            if self._matches(event, filters):
                events.append(event)
        return events

    @staticmethod
    def _price_timestamp(item):
        return datetime.fromisoformat(
            item["effectiveFrom"].replace("Z", "+00:00")
        ).timestamp()

    @staticmethod
    def _price_matches(price, event):
        return (
            event["model"] in [price["model"], *price.get("modelAliases", [])]
            and (event["provider_id"] in price["providerIds"] if price.get("providerIds")
                 else not price["providerId"] or price["providerId"] == event["provider_id"])
        )

    def _allocate_quotas(self, events, quotas, now):
        """按全部调用的时间顺序重放额度；刷新、筛选和分页不会重复扣减。"""
        states = {(q["providerId"], q["model"]): dict(
            q, remainingTokens=max(0, q["totalTokens"] - q["usedTokensBefore"]),
            observedTokens=0, unknownUsageCalls=0,
        ) for q in quotas}
        atoms = []
        for event in events:
            event["quota_free_tokens"] = 0
            if event.get("record_type") == "official_billing_adjustment":
                continue
            requests = event.get("request_details") or []
            measured = [r for r in requests if any(r.get(f, 0) for f in (
                "token_input_other", "token_input_cached", "token_output"))]
            for request in measured:
                request["quota_free_tokens"] = 0
                atoms.append((request["started_at"], event["call_id"], request, event))
            if not measured:
                atoms.append((event["started_at"], event["call_id"], event, event))
        for moment, _, atom, event in sorted(atoms, key=lambda a: (a[0], a[1])):
            key = (atom.get("provider_id") or event["provider_id"],
                   atom.get("model") or event["model"])
            state = states.get(key)
            if not state or moment < self._price_timestamp({"effectiveFrom": state["startsAt"]}):
                continue
            if state["expiresAt"] and moment >= self._price_timestamp({"effectiveFrom": state["expiresAt"]}):
                continue
            tokens = sum(max(0, int(atom.get(f) or 0)) for f in (
                "token_input_other", "token_input_cached", "token_output"))
            if not atom.get("usage_known", False):
                state["unknownUsageCalls"] += 1
            free = min(tokens, state["remainingTokens"])
            state["remainingTokens"] -= free
            state["observedTokens"] += tokens
            atom["quota_free_tokens"] = free
            if atom is not event:
                event["quota_free_tokens"] += free
        for state in states.values():
            expired = state["expiresAt"] and now >= self._price_timestamp(
                {"effectiveFrom": state["expiresAt"]})
            state["expired"] = bool(expired)
            if expired:
                state["remainingTokens"] = 0
        return list(states.values())

    def _event_cost(self, event, prices):
        if event.get("record_type") == "official_billing_adjustment":
            # 官方差额金额固定，不因修改单价或额度而再次估算。
            return event["fixed_amount_cny"], "CNY", "reconciled", None, "official"
        requests = event.get("request_details") or []
        measured = [
            item for item in requests
            if any(item.get(field, 0) for field in
                   ("token_input_other", "token_input_cached", "token_output"))
        ]
        if measured:
            # 各次尝试可能跨峰谷边界，按其真实开始时间计价；外层响应不再重复相加。
            costs = []
            for request in measured:
                attempt = dict(event)
                attempt.update({
                    field: request.get(field, 0) for field in
                    ("token_input_other", "token_input_cached", "token_output")
                })
                attempt.update(
                    started_at=request["started_at"],
                    model=request.get("model") or event["model"],
                    provider_id=request.get("provider_id") or event["provider_id"],
                    usage_known=request.get("usage_known", False),
                    request_details=[],
                    quota_free_tokens=request.get("quota_free_tokens", 0),
                    embedding_input_type=request.get("embedding_input_type")
                    or event.get("embedding_input_type") or "text",
                )
                costs.append(self._event_cost(attempt, prices))
            if any(item[0] is None for item in costs) or len(
                {item[1] for item in costs}
            ) != 1:
                return None, None, "pending", None, None
            amount = sum((Decimal(item[0]) for item in costs), Decimal("0"))
            known = event.get("usage_known", False)
            status = (
                "partial_usage" if not known else
                "free" if all(item[2] == "free" for item in costs) else "configured"
            )
            bands = {item[4] for item in costs}
            return (
                format(amount, "f"), costs[0][1], status, costs[0][3],
                next(iter(bands)) if len(bands) == 1 else "mixed",
            )
        # 未返回用量不等于零费用；有部分用量时仍计算已知部分，并另行提示缺口。
        usage_known = event.get("usage_known", any(
            event[field] for field in
            ("token_input_other", "token_input_cached", "token_output")
        ))
        if not usage_known and not any(
            event[field] for field in
            ("token_input_other", "token_input_cached", "token_output")
        ):
            return None, None, "usage_unknown", None, None
        total_tokens = sum(int(event[f] or 0) for f in (
            "token_input_other", "token_input_cached", "token_output"))
        if usage_known and total_tokens == 0 and event.get("measurement_source") == "livingmemory_journal":
            # 共享缓存明确没有新增消费，不要求先配单价才能确认零费用。
            return "0.00000000", "CNY", "free", None, "shared_cache"
        free_tokens = min(total_tokens, int(event.get("quota_free_tokens", 0)))
        if total_tokens and free_tokens == total_tokens:
            return "0.00000000", "CNY", "free" if usage_known else "partial_usage", None, "quota"
        # 旧 automatic 记录只有路由总用量，不能沿用路由的零单价冒充免费模型。
        if event["model"] == "automatic":
            return None, None, "pending", None, None
        candidates = [
            item
            for item in prices
            if self._price_matches(item, event)
            and self._price_timestamp(item) <= event["started_at"]
        ]
        if not candidates:
            return None, None, "pending", None, None
        price = max(
            candidates,
            key=lambda item: (
                self._price_timestamp(item),
                bool(item["providerId"]),
            ),
        )
        price_band = "flat"
        if price["pricingMode"] == "embedding":
            # 多条文本的实时请求仍是文本输入，只有明确标记的 Batch File 才用批量价。
            input_type = event.get("embedding_input_type") or "text"
            if input_type not in {"text", "batch_file"} or event.get("token_output"):
                return None, None, "pending", price["id"], None
            price_band = "embedding_batch_file" if input_type == "batch_file" else "embedding_text"
            rate_field = "inputBatchCnyPerMillion" if input_type == "batch_file" else "inputOtherCnyPerMillion"
            components = (
                ("token_input_other", rate_field),
                ("token_input_cached", rate_field),
            )
        elif price["pricingMode"] == "deepseek_peak":
            utc_time = datetime.fromtimestamp(event["started_at"], timezone.utc)
            local_date = utc_time.astimezone(self.REPORT_TIMEZONE).date().isoformat()
            is_holiday = local_date in price["holidayDates"]
            is_peak_hour = (
                1 <= utc_time.hour < 4 or 6 <= utc_time.hour < 10
            )
            is_peak = utc_time.weekday() < 5 and not is_holiday and is_peak_hour
            price_band = "peak" if is_peak else "off_peak"
            suffix = "PeakPerMillion" if is_peak else "OffPeakPerMillion"
            components = (
                ("token_input_other", f"inputOther{suffix}"),
                ("token_input_cached", f"inputCached{suffix}"),
                ("token_output", f"output{suffix}"),
            )
        else:
            components = (
                ("token_input_other", "inputOtherCnyPerMillion"),
                ("token_input_cached", "inputCachedCnyPerMillion"),
                ("token_output", "outputCnyPerMillion"),
            )
        amount = Decimal("0")
        billed = False
        counted_tokens = 0
        for token_field, price_field in components:
            tokens = int(event[token_field] or 0)
            rate = price.get(price_field)
            if tokens and rate in ("", None):
                return None, None, "pending", price["id"], price_band
            if tokens:
                counted_tokens += tokens
                amount += Decimal(tokens) * Decimal(rate) / Decimal("1000000")
                if Decimal(rate) > 0:
                    billed = True
        amount = amount.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        if free_tokens and total_tokens:
            # 跨额度边界的请求按输入/缓存/输出占比分摊免费 Token；金额仍是估算值。
            amount = (amount * Decimal(total_tokens - free_tokens) / Decimal(total_tokens)).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        price_status = "configured" if billed or not counted_tokens else "free"
        if not usage_known:
            price_status = "partial_usage"
        return (
            format(amount, "f"),
            price["currency"],
            price_status,
            price["id"],
            price_band,
        )

    @staticmethod
    def _percentile(values, percentile):
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
        return ordered[index]

    @staticmethod
    def _empty_group(key):
        return {
            "key": key,
            "calls": 0,
            "failedCalls": 0,
            "inputOther": 0,
            "inputCached": 0,
            "output": 0,
            "totalTokens": 0,
            "knownAmountCny": Decimal("0"),
            "_knownAmounts": {},
            "pricedCalls": 0,
            "unpricedCalls": 0,
            "missingPriceCalls": 0,
            "unpricedTokens": 0,
            "freeCalls": 0,
            "freeTokens": 0,
            "unknownUsageCalls": 0,
            "requestAttempts": 0,
            "_latencies": [],
            "_ttfts": [],
        }

    def _group(self, events, field):
        groups = {}
        for event in events:
            key = event[field]
            group = groups.setdefault(key, self._empty_group(key))
            aggregate = bool(event.get("record_type"))
            if aggregate:
                group["aggregateRecords"] = group.get("aggregateRecords", 0) + 1
            group["calls"] += int(not aggregate)
            group["failedCalls"] += int(event["status"] != "completed")
            group["unknownUsageCalls"] += int(not event["usage_known"])
            group["missingPriceCalls"] += int(event["price_status"] == "pending")
            group["requestAttempts"] += len(event["request_details"])
            group["inputOther"] += event["token_input_other"]
            group["inputCached"] += event["token_input_cached"]
            group["output"] += event["token_output"]
            group["totalTokens"] += event["total_tokens"]
            group["freeTokens"] += event.get("free_tokens", 0)
            if event["latency_ms"] is not None:
                group["_latencies"].append(event["latency_ms"])
            if event["ttft_ms"] is not None:
                group["_ttfts"].append(event["ttft_ms"])
            if event["amount"] is None:
                group["unpricedCalls"] += 1
                group["unpricedTokens"] += event.get("billable_tokens", event["total_tokens"])
            else:
                group["pricedCalls"] += int(not aggregate)
                if event["price_status"] == "free":
                    group["freeCalls"] += 1
                currency = event["amount_currency"]
                group["_knownAmounts"][currency] = (
                    group["_knownAmounts"].get(currency, Decimal("0"))
                    + Decimal(event["amount"])
                )
        output = []
        for group in groups.values():
            latencies = group.pop("_latencies")
            ttfts = group.pop("_ttfts")
            known_amounts = group.pop("_knownAmounts")
            group["knownAmounts"] = {
                currency: format(
                    amount.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP),
                    "f",
                )
                for currency, amount in sorted(known_amounts.items())
            }
            group["knownAmountCny"] = group["knownAmounts"].get("CNY", "0.000000")
            group["latencyP50Ms"] = self._percentile(latencies, 0.50)
            group["latencyP95Ms"] = self._percentile(latencies, 0.95)
            group["latencyP99Ms"] = self._percentile(latencies, 0.99)
            group["ttftP95Ms"] = self._percentile(ttfts, 0.95)
            output.append(group)
        return sorted(
            output,
            key=lambda item: (item["totalTokens"], item["calls"]),
            reverse=True,
        )

    @staticmethod
    def _model_events(events):
        """按供应商请求的实际模型投影；原调用保留，Token 与额度不重新扣减。"""
        output = []
        token_fields = ("token_input_other", "token_input_cached", "token_output")
        for event in events:
            requests = event.get("request_details") or []
            if not requests:
                output.append(dict(event, requested_model=event["model"],
                                   actual_model_known=event["model"] != "automatic"))
                continue
            groups = {}
            for request in requests:
                model = request.get("model") or event["model"]
                groups.setdefault(model, []).append(request)
            for model, attempts in groups.items():
                row = dict(event, model=model, requested_model=event["model"],
                           actual_model_known=model != "automatic", request_details=attempts)
                row.update({f: sum(r.get(f, 0) for r in attempts) for f in token_fields})
                row["quota_free_tokens"] = sum(r.get("quota_free_tokens", 0) for r in attempts)
                row["usage_known"] = all(r.get("usage_known", False) for r in attempts)
                if event.get("measurement_source") == "livingmemory_journal":
                    row["usage_known"] = row["usage_known"] and event["usage_known"]
                row["unknown_requests"] = sum(not r.get("usage_known", False) for r in attempts)
                row["attempts"] = len(attempts)
                output.append(row)
        return output

    def _configured_usage_models(self):
        """报表与计费窗口使用相同候选，避免向量等类型只能查询不能配置。"""
        return {p["model"] for p in self.available_models().get("models", [])}

    def _models_by_period(self, events, field):
        """完整实际模型汇总供堆叠图使用；不依赖限量/分页的调用明细。"""
        periods = {}
        for event in events:
            if field == "hour" and event.get("record_type"):
                continue
            periods.setdefault(event[field], []).append(event)
        return [{"key": key, "models": self._group(rows, "model")}
                for key, rows in sorted(periods.items())]

    def report(self, query):
        range_name = str(query.get("range", "24h"))
        if range_name not in {*self.RANGE_SECONDS, "today", "yesterday", "all"}:
            raise ValueError("unsupported usage range")
        now = time.time()
        if range_name == "today":
            local_now = datetime.fromtimestamp(now, self.REPORT_TIMEZONE)
            cutoff = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()
        elif range_name == "yesterday":
            # 昨日 = 本地自然日的上一天，窗口右端固定在今天 00:00
            local_now = datetime.fromtimestamp(now, self.REPORT_TIMEZONE)
            today_start = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).timestamp()
            cutoff = today_start - 86400
            now = today_start
        elif range_name == "all":
            cutoff = 0
        else:
            cutoff = now - self.RANGE_SECONDS[range_name]
        filters = {
            field: str(query.get(field) or "").strip()
            for field in ("plugin_id", "model", "status")
        }
        maximum_limit = 100000 if query.get("_export") else 1000
        limit = min(maximum_limit, max(1, int(query.get("limit", 100))))
        offset = max(0, int(query.get("offset", 0)))
        boundary = self._installed_at()
        config = self.pricing()
        prices = config["history"] + config["prices"]
        quotas = config["freeQuotas"]
        # 免费额度按生命周期累计，不能只从当前报表窗口或选中的插件开始扣减。
        ledger_cutoff = min([cutoff] + [
            self._price_timestamp({"effectiveFrom": q["startsAt"]}) for q in quotas
        ])
        events = self._telemetry_events(ledger_cutoff, now, {})
        events.extend(self._historical_events(ledger_cutoff, boundary, {}))
        events = [e for e in events if e["started_at"] <= now]
        quota_states = self._allocate_quotas(events, quotas, now)
        events = [e for e in events if e["started_at"] >= cutoff]
        model_events = self._model_events(events)
        all_options = {
            "plugins": sorted({e["plugin_id"] for e in events if e["started_at"] >= cutoff}),
            "models": sorted({e["model"] for e in model_events} | self._configured_usage_models()),
            "statuses": sorted({e["status"] for e in events if e["started_at"] >= cutoff}),
        }
        # 筛选实际模型时只使用该模型的请求，跨模型重试不会把其他模型的 Token 带进来。
        for event in events + model_events:
            event["call_kind"] = event.get("call_kind") or "llm"
            event["day"] = datetime.fromtimestamp(
                event["started_at"],
                self.REPORT_TIMEZONE,
            ).date().isoformat()
            event["hour"] = datetime.fromtimestamp(
                event["started_at"],
                self.REPORT_TIMEZONE,
            ).strftime("%Y-%m-%dT%H")
            event["total_tokens"] = (
                event["token_input_other"]
                + event["token_input_cached"]
                + event["token_output"]
            )
            amount, currency, price_status, price_id, price_band = self._event_cost(
                event,
                prices,
            )
            event["amount"] = amount
            event["amount_currency"] = currency
            event["amount_cny"] = amount if currency == "CNY" else None
            event["price_status"] = price_status
            event["price_id"] = price_id
            event["price_band"] = price_band
            event["free_tokens"] = (
                event["total_tokens"] if price_status == "free"
                else event.get("quota_free_tokens", 0)
            )
            event["billable_tokens"] = event["total_tokens"] - event["free_tokens"]
        account_events = [e for e in events if e["provider_id"].startswith("deepseek/")]
        events = [e for e in (model_events if filters["model"] else events)
                  if self._matches(e, filters)]
        model_events = [e for e in model_events if self._matches(e, filters)]
        events.sort(key=lambda item: item["started_at"], reverse=True)
        total_tokens = sum(item["total_tokens"] for item in events)
        known_amounts = {}
        for item in events:
            if item["amount"] is None:
                continue
            currency = item["amount_currency"]
            known_amounts[currency] = (
                known_amounts.get(currency, Decimal("0"))
                + Decimal(item["amount"])
            )
        known_amounts = {
            currency: format(
                amount.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP),
                "f",
            )
            for currency, amount in sorted(known_amounts.items())
        }
        aggregate_events = [item for item in events if item.get("record_type")]
        call_events = [item for item in events if not item.get("record_type")]
        latencies = [item["latency_ms"] for item in events if item["latency_ms"] is not None]
        ttfts = [item["ttft_ms"] for item in events if item["ttft_ms"] is not None]
        unpriced = [item for item in events if item["amount"] is None]
        free_events = [item for item in events if item["price_status"] == "free"]
        free_tokens = sum(item["free_tokens"] for item in events)
        unpriced_tokens = sum(item["billable_tokens"] for item in unpriced)
        browsable_total = (
            len(events)
            if query.get("_export")
            else min(len(events), 1000)
        )
        balance_summary, balance_hours, balance_samples = self._balance_consumption(
            cutoff, now
        )
        # DeepSeek 账户余额只能和 DeepSeek 全连接用量比较，不能与全部供应商费用混算。
        balance_scope = (filters["model"].startswith("deepseek")
                         and not filters["plugin_id"] and not filters["status"])
        if not balance_scope:
            balance_summary = {"available": False, "reason": "仅限 DeepSeek 模型筛选"}
            balance_hours, balance_samples = [], []
        else:
            balance_summary["accountEstimateCny"] = format(sum(
                (Decimal(e["amount"]) for e in account_events if e["amount"] is not None),
                Decimal("0")), "f")
        attributed, orphaned = self._attribute_balance_to_hours(
            account_events, balance_samples
        )
        account_by_hour = {g["key"]: g for g in self._group(account_events, "hour")}
        usage_by_hour = {
            item["key"]: item for item in self._group(call_events, "hour")
        }
        if not balance_hours:
            # 余额采样覆盖不到（如昨日早于首个采样点）时，仍按事件给出小时分布，
            # 只缺「余额实测」一列，前端照旧能画出估算柱。
            balance_hours = []
            for group in sorted(usage_by_hour.values(), key=lambda item: item["key"]):
                moment = datetime.strptime(group["key"], "%Y-%m-%dT%H")
                balance_hours.append(
                    {
                        "key": group["key"],
                        "hour": moment.strftime("%H:00"),
                        "startAt": None,
                        "endAt": None,
                        "startBalance": None,
                        "endBalance": None,
                    }
                )
        hourly = []
        for slot in balance_hours:
            group = usage_by_hour.get(slot["key"])
            consumed = attributed.get(slot["key"])
            orphan = orphaned.get(slot["key"], 0.0)
            hourly.append(
                {
                    "key": slot["key"],
                    "hour": slot["hour"],
                    "startAt": slot["startAt"],
                    "endAt": slot["endAt"],
                    "startBalance": slot["startBalance"],
                    "endBalance": slot["endBalance"],
                    "balanceConsumed": (
                        None
                        if consumed is None
                        else format(
                            Decimal(str(consumed)).quantize(
                                Decimal("0.01"), rounding=ROUND_HALF_UP
                            ),
                            "f",
                        )
                    ),
                    "unattributed": (
                        None
                        if not orphan
                        else format(
                            Decimal(str(orphan)).quantize(
                                Decimal("0.01"), rounding=ROUND_HALF_UP
                            ),
                            "f",
                        )
                    ),
                    "calls": group["calls"] if group else 0,
                    "failedCalls": group["failedCalls"] if group else 0,
                    "totalTokens": group["totalTokens"] if group else 0,
                    "freeTokens": group["freeTokens"] if group else 0,
                    "amountCny": group["knownAmountCny"] if group else "0.000000",
                    "accountAmountCny": account_by_hour.get(slot["key"], {}).get("knownAmountCny"),
                    "missingPriceCalls": group["missingPriceCalls"] if group else 0,
                    "unknownUsageCalls": group["unknownUsageCalls"] if group else 0,
                }
            )
        hourly.sort(key=lambda item: item["key"], reverse=True)
        if balance_summary.get("available"):
            balance_summary["unattributed"] = format(
                Decimal(str(sum(orphaned.values()))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
                "f",
            )
        payload = {
            "ok": True,
            "range": range_name,
            "from": cutoff,
            "to": now,
            "filters": filters,
            "collection": {
                "installedAt": boundary,
                "historicalAttribution": "unattributed",
                "postInstallSource": "plugin_usage.db",
                "storesPromptOrResponse": False,
                "actualModelAttribution": "supplier_request_model",
                "legacyRouterModelUnknown": sum(
                    e["model"] == "automatic" and not e["request_details"] for e in events),
            },
            "summary": {
                "calls": len(call_events),
                "failedCalls": sum(
                    item["status"] != "completed" for item in events
                ),
                "inputOther": sum(item["token_input_other"] for item in events),
                "inputCached": sum(item["token_input_cached"] for item in events),
                "output": sum(item["token_output"] for item in events),
                "totalTokens": total_tokens,
                "knownAmounts": known_amounts,
                "knownAmountCny": known_amounts.get("CNY", "0.000000"),
                "pricedCalls": sum(e["amount"] is not None for e in call_events),
                "unpricedCalls": len(unpriced),
                "missingPriceCalls": sum(item["price_status"] == "pending" for item in events),
                "unpricedTokens": unpriced_tokens,
                "freeCalls": len(free_events),
                "freeTokens": free_tokens,
                "billedCalls": sum(e["amount"] is not None and e["price_status"] != "free" for e in call_events),
                "billedTokens": total_tokens - free_tokens - unpriced_tokens,
                "unknownUsageCalls": sum(not item["usage_known"] for item in events),
                "unknownUsageRequests": sum(item["unknown_requests"] for item in events),
                "requestAttempts": sum(len(item["request_details"]) for item in events),
                "latencyP50Ms": self._percentile(latencies, 0.50),
                "latencyP95Ms": self._percentile(latencies, 0.95),
                "latencyP99Ms": self._percentile(latencies, 0.99),
                "ttftP95Ms": self._percentile(ttfts, 0.95),
            },
            "balance": balance_summary,
            "search": self._search_summary(cutoff, now),
            "freeQuotas": quota_states,
            "byHour": hourly,
            "byDay": sorted(
                self._group(events, "day"),
                key=lambda item: item["key"],
                reverse=True,
            ),
            "byPlugin": self._group(events, "plugin_id"),
            "byModel": self._group(model_events, "model"),
            "byKind": self._group(events, "call_kind"),
            "byDayModel": self._models_by_period(model_events, "day"),
            "byHourModel": self._models_by_period(model_events, "hour"),
            "options": all_options,
            "pagination": {
                "total": browsable_total,
                "availableTotal": len(events),
                "limit": limit,
                "offset": offset,
                "capped": len(events) > browsable_total,
            },
            "details": events[offset : min(offset + limit, browsable_total)],
        }
        if aggregate_events:
            payload["summary"]["aggregateRecords"] = len(aggregate_events)
            payload["reconciliation"] = {
                "records": len(aggregate_events),
                "totalTokens": sum(e["total_tokens"] for e in aggregate_events),
                "amountCny": format(sum((Decimal(e["amount"]) for e in aggregate_events
                                        if e["amount_currency"] == "CNY"), Decimal("0")), "f"),
                "excludedFromHourly": True,
                "note": "日总含网关汇总及官方差额补录；无逐笔时间的记录不计入小时图、调用次数和延迟。",
            }
        monitoring = self._usage_monitoring(now)
        if monitoring is not None:
            payload["usageMonitoring"] = monitoring
        return payload

    def export_csv(self, query):
        query = dict(query)
        query["limit"] = 100000
        query["_export"] = True
        report = self.report(query)
        fields = [
            "started_at",
            "day",
            "plugin_id",
            "route",
            "source",
            "provider_id",
            "model",
            "requested_model",
            "call_kind",
            "status",
            "token_input_other",
            "token_input_cached",
            "token_output",
            "total_tokens",
            "latency_ms",
            "ttft_ms",
            "attempts",
            "usage_known",
            "unknown_requests",
            "request_details",
            "amount",
            "amount_currency",
            "amount_cny",
            "price_status",
            "price_band",
            "free_tokens",
            "billable_tokens",
            "historical",
            "call_id",
            "record_type",
            "note",
            "period_start",
            "period_end",
            "fixed_amount_cny",
        ]
        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for event in report["details"]:
            event = dict(event)
            event["request_details"] = json.dumps(
                event["request_details"], ensure_ascii=False, separators=(",", ":")
            )
            writer.writerow(
                {
                    key: (
                        f"'{value}"
                        if isinstance(value, str)
                        and value.startswith(("=", "+", "-", "@"))
                        else value
                    )
                    for key, value in event.items()
                }
            )
        return output.getvalue().encode("utf-8")


class BalanceService:
    REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")

    def __init__(self, credentials_path, cache_seconds=300, snapshot_store=None):
        self.credentials_path = Path(credentials_path)
        self.cache_seconds = cache_seconds
        self.snapshot_store = snapshot_store
        self.lock = threading.Lock()
        self.cached_at = 0
        self.cached_mtime = None
        self.cached_payload = None

    def _credentials(self):
        try:
            stat = self.credentials_path.stat()
            payload = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            return self._normalize_config(payload), stat.st_mtime_ns
        except FileNotFoundError:
            return self._normalize_config({}), None

    @staticmethod
    def _normalize_config(payload):
        if not isinstance(payload, dict):
            raise ProviderConfigError("provider credentials must be an object")
        if payload.get("version") == 2:
            deepseek = payload.get("deepseek") or {}
            volcengine = payload.get("volcengine") or {}
            bailian = payload.get("bailian") or {}
            custom = payload.get("custom") or []
        else:
            deepseek = {"apiKey": payload.get("deepseekApiKey", "")}
            volcengine = {
                "accessKeyId": payload.get("volcAccessKeyId", ""),
                "secretAccessKey": payload.get("volcSecretAccessKey", ""),
            }
            bailian = {
                "accessKeyId": payload.get("bailianAccessKeyId", ""),
                "accessKeySecret": payload.get("bailianAccessKeySecret", ""),
            }
            custom = payload.get("custom") or []
        return {
            "version": 2,
            "deepseek": deepseek if isinstance(deepseek, dict) else {},
            "volcengine": volcengine if isinstance(volcengine, dict) else {},
            "bailian": bailian if isinstance(bailian, dict) else {},
            "custom": custom if isinstance(custom, list) else [],
        }

    @staticmethod
    def _text(value, field, maximum, required=False):
        if not isinstance(value, str):
            raise ProviderConfigError(f"{field} must be a string")
        value = value.strip()
        if required and not value:
            raise ProviderConfigError(f"{field} is required")
        if len(value) > maximum:
            raise ProviderConfigError(f"{field} is too long")
        return value

    @staticmethod
    def _secret(section, field, current, clear_field):
        if section.get(clear_field) is True:
            return ""
        value = section.get(field, "")
        if value in ("", None):
            return str(current or "")
        return BalanceService._text(value, field, 2048, required=True)

    @staticmethod
    def _validate_endpoint(endpoint):
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ProviderConfigError(
                "custom endpoint must be an HTTPS URL without credentials or fragments"
            )
        return parsed

    @staticmethod
    def _validate_json_path(path, field, required=False):
        path = BalanceService._text(path, field, 256, required=required)
        if path and not JSON_PATH_RE.fullmatch(path):
            raise ProviderConfigError(
                f"{field} must use dot notation such as data.balance"
            )
        return path

    def config_summary(self, reveal=False):
        with self.lock:
            config, _ = self._credentials()
            deepseek = config["deepseek"]
            volcengine = config["volcengine"]
            bailian = config["bailian"]
            return {
                "ok": True,
                "deepseek": {
                    "configured": bool(deepseek.get("apiKey")),
                    **({"apiKey": str(deepseek.get("apiKey") or "")} if reveal else {}),
                },
                "volcengine": {
                    "configured": bool(
                        volcengine.get("accessKeyId")
                        and volcengine.get("secretAccessKey")
                    ),
                    **(
                        {
                            "accessKeyId": str(volcengine.get("accessKeyId") or ""),
                            "secretAccessKey": str(
                                volcengine.get("secretAccessKey") or ""
                            ),
                        }
                        if reveal
                        else {}
                    ),
                },
                "bailian": {
                    "configured": bool(
                        bailian.get("accessKeyId") and bailian.get("accessKeySecret")
                    ),
                    **(
                        {
                            "accessKeyId": str(bailian.get("accessKeyId") or ""),
                            "accessKeySecret": str(
                                bailian.get("accessKeySecret") or ""
                            ),
                        }
                        if reveal
                        else {}
                    ),
                },
                "custom": [
                    {
                        "id": str(item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "endpoint": str(item.get("endpoint") or ""),
                        "headerName": str(item.get("headerName") or ""),
                        "credentialConfigured": bool(item.get("headerValue")),
                        **(
                            {"headerValue": str(item.get("headerValue") or "")}
                            if reveal
                            else {}
                        ),
                        "amountPath": str(item.get("amountPath") or ""),
                        "currencyPath": str(item.get("currencyPath") or ""),
                        "detailPath": str(item.get("detailPath") or ""),
                        "enabled": item.get("enabled") is not False,
                    }
                    for item in config["custom"][:MAX_CUSTOM_PROVIDERS]
                    if isinstance(item, dict)
                ],
                "limits": {"customProviders": MAX_CUSTOM_PROVIDERS},
            }

    def update_config(self, update):
        if not isinstance(update, dict):
            raise ProviderConfigError("configuration payload must be an object")
        unknown = set(update) - {"deepseek", "volcengine", "bailian", "custom"}
        if unknown:
            raise ProviderConfigError("configuration payload contains unsupported fields")
        deepseek_update = update.get("deepseek") or {}
        volcengine_update = update.get("volcengine") or {}
        bailian_update = update.get("bailian") or {}
        custom_update = update.get("custom") or []
        if not isinstance(deepseek_update, dict) or not isinstance(volcengine_update, dict):
            raise ProviderConfigError("built-in provider configuration must be an object")
        if not isinstance(bailian_update, dict):
            raise ProviderConfigError("bailian configuration must be an object")
        if set(deepseek_update) - {"apiKey", "clearApiKey"}:
            raise ProviderConfigError("DeepSeek configuration contains unsupported fields")
        if set(bailian_update) - {
            "accessKeyId",
            "accessKeySecret",
            "clearAccessKeyId",
            "clearAccessKeySecret",
        }:
            raise ProviderConfigError("Bailian configuration contains unsupported fields")
        if set(volcengine_update) - {
            "accessKeyId",
            "secretAccessKey",
            "clearAccessKeyId",
            "clearSecretAccessKey",
        }:
            raise ProviderConfigError("Volcengine configuration contains unsupported fields")
        for section, fields in (
            (deepseek_update, ("clearApiKey",)),
            (
                volcengine_update,
                ("clearAccessKeyId", "clearSecretAccessKey"),
            ),
            (
                bailian_update,
                ("clearAccessKeyId", "clearAccessKeySecret"),
            ),
        ):
            for field in fields:
                if field in section and not isinstance(section[field], bool):
                    raise ProviderConfigError(f"{field} must be a boolean")
        if not isinstance(custom_update, list):
            raise ProviderConfigError("custom provider configuration must be a list")
        if len(custom_update) > MAX_CUSTOM_PROVIDERS:
            raise ProviderConfigError(
                f"no more than {MAX_CUSTOM_PROVIDERS} custom providers are allowed"
            )

        with self.lock:
            current, _ = self._credentials()
            current_custom = {
                str(item.get("id")): item
                for item in current["custom"]
                if isinstance(item, dict) and item.get("id")
            }
            next_config = {
                "version": 2,
                "deepseek": {
                    "apiKey": self._secret(
                        deepseek_update,
                        "apiKey",
                        current["deepseek"].get("apiKey"),
                        "clearApiKey",
                    )
                },
                "volcengine": {
                    "accessKeyId": self._secret(
                        volcengine_update,
                        "accessKeyId",
                        current["volcengine"].get("accessKeyId"),
                        "clearAccessKeyId",
                    ),
                    "secretAccessKey": self._secret(
                        volcengine_update,
                        "secretAccessKey",
                        current["volcengine"].get("secretAccessKey"),
                        "clearSecretAccessKey",
                    ),
                },
                "bailian": {
                    "accessKeyId": self._secret(
                        bailian_update,
                        "accessKeyId",
                        current["bailian"].get("accessKeyId"),
                        "clearAccessKeyId",
                    ),
                    "accessKeySecret": self._secret(
                        bailian_update,
                        "accessKeySecret",
                        current["bailian"].get("accessKeySecret"),
                        "clearAccessKeySecret",
                    ),
                },
                "custom": [],
            }
            identifiers = set()
            for index, item in enumerate(custom_update):
                if not isinstance(item, dict):
                    raise ProviderConfigError(f"custom provider {index + 1} must be an object")
                allowed_fields = {
                    "id",
                    "name",
                    "endpoint",
                    "headerName",
                    "headerValue",
                    "amountPath",
                    "currencyPath",
                    "detailPath",
                    "enabled",
                    "clearHeaderValue",
                }
                if set(item) - allowed_fields:
                    raise ProviderConfigError(
                        f"custom provider {index + 1} contains unsupported fields"
                    )
                if "enabled" in item and not isinstance(item["enabled"], bool):
                    raise ProviderConfigError("enabled must be a boolean")
                if "clearHeaderValue" in item and not isinstance(
                    item["clearHeaderValue"], bool
                ):
                    raise ProviderConfigError("clearHeaderValue must be a boolean")
                identifier = str(item.get("id") or "").strip().lower()
                if not identifier:
                    identifier = f"custom-{secrets.token_hex(5)}"
                if not IDENTIFIER_RE.fullmatch(identifier) or identifier in identifiers:
                    raise ProviderConfigError("custom provider IDs must be unique URL-safe names")
                identifiers.add(identifier)
                previous = current_custom.get(identifier, {})
                name = self._text(item.get("name", ""), "name", 80, required=True)
                endpoint = self._text(
                    item.get("endpoint", ""),
                    "endpoint",
                    2048,
                    required=True,
                )
                self._validate_endpoint(endpoint)
                header_name = self._text(item.get("headerName", ""), "headerName", 64)
                if header_name:
                    if (
                        not HEADER_NAME_RE.fullmatch(header_name)
                        or header_name.lower() in FORBIDDEN_HEADERS
                    ):
                        raise ProviderConfigError("custom authentication header is not allowed")
                header_value = self._secret(
                    item,
                    "headerValue",
                    previous.get("headerValue"),
                    "clearHeaderValue",
                )
                if header_value and not header_name:
                    raise ProviderConfigError(
                        "headerName is required when an authentication value is configured"
                    )
                next_config["custom"].append(
                    {
                        "id": identifier,
                        "name": name,
                        "endpoint": endpoint,
                        "headerName": header_name,
                        "headerValue": header_value,
                        "amountPath": self._validate_json_path(
                            item.get("amountPath", ""),
                            "amountPath",
                            required=True,
                        ),
                        "currencyPath": self._validate_json_path(
                            item.get("currencyPath", ""),
                            "currencyPath",
                        ),
                        "detailPath": self._validate_json_path(
                            item.get("detailPath", ""),
                            "detailPath",
                        ),
                        "enabled": item.get("enabled") is not False,
                    }
                )

            self.credentials_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.credentials_path.with_suffix(
                self.credentials_path.suffix + ".tmp"
            )
            temporary.write_text(
                json.dumps(next_config, ensure_ascii=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.credentials_path)
            if os.geteuid() == 0:
                os.chown(self.credentials_path, 0, 0)
            self.cached_payload = None
            self.cached_at = 0
            self.cached_mtime = None
        return self.config_summary()

    @staticmethod
    def _request_json(request, timeout=6):
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("provider response is too large")
            return json.loads(body.decode("utf-8", "replace"))

    @staticmethod
    def _public_endpoint(endpoint):
        parsed = BalanceService._validate_endpoint(endpoint)
        try:
            addresses = {
                result[4][0]
                for result in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise OSError("custom provider hostname could not be resolved") from exc
        if not addresses:
            raise OSError("custom provider hostname has no address")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ProviderConfigError(
                    "custom provider endpoint must resolve to a public address"
                )
        return endpoint

    @staticmethod
    def _json_path(payload, path):
        value = payload
        for segment in path.split("."):
            if isinstance(value, dict):
                if segment not in value:
                    raise ValueError(f"response path not found: {path}")
                value = value[segment]
            elif isinstance(value, list) and segment.isdigit():
                index = int(segment)
                if index >= len(value):
                    raise ValueError(f"response path not found: {path}")
                value = value[index]
            else:
                raise ValueError(f"response path not found: {path}")
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError(f"response path must resolve to a scalar: {path}")
        return str(value)

    def _custom(self, provider):
        endpoint = self._public_endpoint(provider["endpoint"])
        headers = {
            "Accept": "application/json",
            "User-Agent": "BotManager/1.0",
        }
        if provider.get("headerName") and provider.get("headerValue"):
            headers[provider["headerName"]] = provider["headerValue"]
        request = urllib.request.Request(endpoint, headers=headers)
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=6) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("provider response is too large")
        payload = json.loads(body.decode("utf-8", "replace"))
        return {
            "configured": True,
            "status": "ok",
            "id": provider["id"],
            "name": provider["name"],
            "amount": self._json_path(payload, provider["amountPath"]),
            "currency": (
                self._json_path(payload, provider["currencyPath"])
                if provider.get("currencyPath")
                else ""
            ),
            "detail": (
                self._json_path(payload, provider["detailPath"])
                if provider.get("detailPath")
                else ""
            ),
        }

    def _deepseek(self, api_key):
        if not api_key:
            return {"configured": False, "status": "unconfigured"}
        request = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "BotManager/1.0",
            },
        )
        payload = self._request_json(request)
        balances = []
        for item in payload.get("balance_infos") or []:
            if not isinstance(item, dict):
                continue
            balances.append(
                {
                    "currency": str(item.get("currency") or ""),
                    "total": str(item.get("total_balance") or ""),
                    "granted": str(item.get("granted_balance") or ""),
                    "toppedUp": str(item.get("topped_up_balance") or ""),
                }
            )
        return {
            "configured": True,
            "status": "ok",
            "available": bool(payload.get("is_available")),
            "balances": balances,
        }

    @staticmethod
    def _volc_authorization(access_key, secret_key, x_date):
        host = "billing.volcengineapi.com"
        region = "cn-beijing"
        service = "billing"
        query = "Action=QueryBalanceAcct&Version=2022-01-01"
        signed_headers = "host;x-date"
        canonical_headers = f"host:{host}\nx-date:{x_date}\n"
        empty_hash = hashlib.sha256(b"").hexdigest()
        canonical_request = (
            f"GET\n/\n{query}\n{canonical_headers}\n"
            f"{signed_headers}\n{empty_hash}"
        )
        date_stamp = x_date[:8]
        scope = f"{date_stamp}/{region}/{service}/request"
        string_to_sign = (
            "HMAC-SHA256\n"
            f"{x_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        def sign(key, value):
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

        date_key = sign(secret_key.encode("utf-8"), date_stamp)
        region_key = sign(date_key, region)
        service_key = sign(region_key, service)
        signing_key = sign(service_key, "request")
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    # ------------------------------------------------------------ 阿里云百炼
    # 百炼本身没有公开的余额接口，能查的是阿里云账户余额（BSS OpenAPI）。
    # 注意口径：这是整个阿里云账户的可用额度，不是百炼独占，也不含资源包与免费额度。
    @staticmethod
    def _aliyun_percent(value):
        """阿里云 RPC 签名要求的百分号编码：空格用 %20，* 编码，~ 保留。"""
        encoded = urllib.parse.quote(str(value), safe="")
        return encoded.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")

    def _aliyun_rpc(self, access_key, access_key_secret, action, extra=None):
        params = {
            "AccessKeyId": access_key,
            "Action": action,
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": secrets.token_hex(16),
            "SignatureVersion": "1.0",
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": "2017-12-14",
        }
        params.update(extra or {})
        canonical = "&".join(
            f"{self._aliyun_percent(key)}={self._aliyun_percent(value)}"
            for key, value in sorted(params.items())
        )
        string_to_sign = "GET&%2F&" + self._aliyun_percent(canonical)
        digest = hmac.new(
            (access_key_secret + "&").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        params["Signature"] = base64.b64encode(digest).decode("utf-8")
        query = "&".join(
            f"{self._aliyun_percent(key)}={self._aliyun_percent(value)}"
            for key, value in params.items()
        )
        request = urllib.request.Request(
            f"https://business.aliyuncs.com/?{query}",
            headers={"Accept": "application/json", "User-Agent": "BotManager/1.0"},
        )
        payload = self._request_json(request)
        if payload.get("Success") is False:
            raise RuntimeError(
                str(payload.get("Code") or payload.get("Message") or "provider error")
            )
        return payload

    def _bailian(self, access_key, access_key_secret):
        if not access_key or not access_key_secret:
            return {"configured": False, "status": "unconfigured"}
        payload = self._aliyun_rpc(access_key, access_key_secret, "QueryAccountBalance")
        data = payload.get("Data") or {}
        return {
            "configured": True,
            "status": "ok",
            "currency": str(data.get("Currency") or "CNY"),
            "availableBalance": str(data.get("AvailableAmount") or ""),
            "cashBalance": str(data.get("AvailableCashAmount") or ""),
            "creditAmount": str(data.get("CreditAmount") or ""),
            "quotaLimit": str(data.get("QuotaLimit") or ""),
        }

    def _volcengine(self, access_key, secret_key):
        if not access_key or not secret_key:
            return {"configured": False, "status": "unconfigured"}
        x_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        authorization = self._volc_authorization(access_key, secret_key, x_date)
        request = urllib.request.Request(
            (
                "https://billing.volcengineapi.com/"
                "?Action=QueryBalanceAcct&Version=2022-01-01"
            ),
            headers={
                "Accept": "application/json",
                "Authorization": authorization,
                "Host": "billing.volcengineapi.com",
                "X-Date": x_date,
                "User-Agent": "BotManager/1.0",
            },
        )
        payload = self._request_json(request)
        metadata = payload.get("ResponseMetadata") or {}
        if metadata.get("Error"):
            raise RuntimeError(str(metadata["Error"].get("Code") or "provider error"))
        result = payload.get("Result") or {}
        return {
            "configured": True,
            "status": "ok",
            "currency": "CNY",
            "availableBalance": str(result.get("AvailableBalance") or ""),
            "cashBalance": str(result.get("CashBalance") or ""),
            "creditLimit": str(result.get("CreditLimit") or ""),
            "freezeAmount": str(result.get("FreezeAmount") or ""),
            "arrearsBalance": str(result.get("ArrearsBalance") or ""),
        }

    @staticmethod
    def _safe_query(callback, configured):
        if not configured:
            return {"configured": False, "status": "unconfigured"}
        try:
            return callback()
        except urllib.error.HTTPError as exc:
            status = "auth_error" if exc.code in (401, 403) else "provider_error"
            return {"configured": True, "status": status}
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            RuntimeError,
            urllib.error.URLError,
        ):
            return {"configured": True, "status": "provider_error"}

    def get(self, force=False):
        with self.lock:
            credentials, mtime = self._credentials()
            now = int(time.time())
            cache_valid = (
                self.cached_payload is not None
                and self.cached_mtime == mtime
                and now - self.cached_at < self.cache_seconds
            )
            if cache_valid and not force:
                return self.cached_payload

            deepseek_key = str(
                credentials["deepseek"].get("apiKey")
                or os.environ.get("DEEPSEEK_API_KEY")
                or ""
            ).strip()
            volc_access_key = str(
                credentials["volcengine"].get("accessKeyId")
                or os.environ.get("VOLC_ACCESS_KEY_ID")
                or ""
            ).strip()
            volc_secret_key = str(
                credentials["volcengine"].get("secretAccessKey")
                or os.environ.get("VOLC_SECRET_ACCESS_KEY")
                or ""
            ).strip()
            bailian_access_key = str(
                credentials["bailian"].get("accessKeyId")
                or os.environ.get("BAILIAN_ACCESS_KEY_ID")
                or ""
            ).strip()
            bailian_secret_key = str(
                credentials["bailian"].get("accessKeySecret")
                or os.environ.get("BAILIAN_ACCESS_KEY_SECRET")
                or ""
            ).strip()
            custom_providers = [
                item
                for item in credentials["custom"][:MAX_CUSTOM_PROVIDERS]
                if isinstance(item, dict) and item.get("enabled") is not False
            ]
            with ThreadPoolExecutor(
                max_workers=max(2, min(6, 2 + len(custom_providers)))
            ) as executor:
                deepseek = executor.submit(
                    self._safe_query,
                    lambda: self._deepseek(deepseek_key),
                    bool(deepseek_key),
                )
                volcengine = executor.submit(
                    self._safe_query,
                    lambda: self._volcengine(volc_access_key, volc_secret_key),
                    bool(volc_access_key and volc_secret_key),
                )
                bailian = executor.submit(
                    self._safe_query,
                    lambda: self._bailian(bailian_access_key, bailian_secret_key),
                    bool(bailian_access_key and bailian_secret_key),
                )
                custom = [
                    (
                        provider,
                        executor.submit(
                            self._safe_query,
                            lambda current=provider: self._custom(current),
                            True,
                        ),
                    )
                    for provider in custom_providers
                ]
                payload = {
                    "ok": True,
                    "timestamp": now,
                    "cacheSeconds": self.cache_seconds,
                    "deepseek": deepseek.result(),
                    "volcengine": volcengine.result(),
                    "bailian": bailian.result(),
                    "custom": [
                        {
                            **future.result(),
                            "id": str(provider.get("id") or ""),
                            "name": str(provider.get("name") or ""),
                        }
                        for provider, future in custom
                    ],
                }
            deepseek_payload = payload["deepseek"]
            if (
                self.snapshot_store
                and deepseek_payload.get("status") == "ok"
                and deepseek_payload.get("balances")
            ):
                self.snapshot_store.record_balances(
                    "deepseek",
                    deepseek_payload["balances"],
                    now,
                )
                local_now = datetime.fromtimestamp(now, self.REPORT_TIMEZONE)
                day_start = local_now.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                ).timestamp()
                deepseek_payload["balanceObservation"] = (
                    self.snapshot_store.balance_observation(
                        "deepseek",
                        "CNY",
                        day_start,
                    )
                )
            self.cached_payload = payload
            self.cached_at = now
            self.cached_mtime = mtime
            return payload

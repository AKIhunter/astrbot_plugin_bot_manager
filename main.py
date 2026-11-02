import asyncio
import contextvars
import functools
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from .runtime_paths import DATABASE_PATH, STATE_PATH, TOKEN_PATH, SERVICE_BASE


NOTIFICATION_STATE_PATH = STATE_PATH
INTERNAL_TOKEN_PATH = TOKEN_PATH
BALANCE_ENDPOINT = SERVICE_BASE + "/api/internal/deepseek-balance"
PLUGIN_PATH_RE = re.compile(r"/data/plugins/([^/]+)/(.+\.py)$")
COLLECTOR_PLUGIN_ID = "astrbot_plugin_usage_telemetry"
CALL_CONTEXT = contextvars.ContextVar("bot_manager_usage_call", default=None)
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
LOW_BALANCE_THRESHOLD = Decimal("5.00")
MINIMUM_RECHARGE_INCREMENT = Decimal("0.01")
BALANCE_CHECK_SECONDS = 5 * 60
CATALOG_PATH = NOTIFICATION_STATE_PATH.with_name("command-catalog.json")
TERMINAL_NOTIFICATION_STATES = {"sent", "unknown"}


class UsageStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    call_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL,
                    plugin_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    source TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    token_input_other INTEGER NOT NULL DEFAULT 0,
                    token_input_cached INTEGER NOT NULL DEFAULT 0,
                    token_output INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    ttft_ms INTEGER,
                    error_type TEXT,
                    attempts INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS usage_events_started_at
                    ON usage_events(started_at);
                CREATE INDEX IF NOT EXISTS usage_events_plugin_started
                    ON usage_events(plugin_id, started_at);
                CREATE INDEX IF NOT EXISTS usage_events_model_started
                    ON usage_events(model, started_at);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO metadata(key, value)
                VALUES ('installed_at', ?)
                """,
                (str(time.time()),),
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(usage_events)")
            }
            for name, definition in (
                ("usage_known", "INTEGER"),
                ("request_details", "TEXT NOT NULL DEFAULT '[]'"),
                ("call_kind", "TEXT NOT NULL DEFAULT 'llm'"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE usage_events ADD COLUMN {name} {definition}"
                    )
            # 重启后保留已落库的调用，不能让中途被结束的记录一直显示执行中。
            connection.execute(
                "UPDATE usage_events SET status='incomplete', "
                "error_type='CollectorRestart', usage_known=0 "
                "WHERE status='in_progress'"
            )
        os.chmod(self.path, 0o600)

    def record(self, event):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO usage_events (
                    call_id, started_at, ended_at, plugin_id, route, source,
                    provider_id, model, token_input_other, token_input_cached,
                    token_output, status, latency_ms, ttft_ms, error_type, attempts,
                    usage_known, request_details, call_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    ended_at=excluded.ended_at,
                    token_input_other=excluded.token_input_other,
                    token_input_cached=excluded.token_input_cached,
                    token_output=excluded.token_output,
                    status=excluded.status, latency_ms=excluded.latency_ms,
                    ttft_ms=excluded.ttft_ms, error_type=excluded.error_type,
                    attempts=excluded.attempts, usage_known=excluded.usage_known,
                    request_details=excluded.request_details
                """,
                (
                    event["call_id"],
                    event["started_at"],
                    event["ended_at"],
                    event["plugin_id"],
                    event["route"],
                    event["source"],
                    event["provider_id"],
                    event["model"],
                    event["token_input_other"],
                    event["token_input_cached"],
                    event["token_output"],
                    event["status"],
                    event["latency_ms"],
                    event["ttft_ms"],
                    event["error_type"],
                    event["attempts"],
                    int(event["usage_known"]),
                    json.dumps(event["request_details"], separators=(",", ":")),
                    event.get("call_kind", "llm"),
                ),
            )

    def mark_ready(self, source_path):
        state = {
            "pid": os.getpid(),
            "enabled_at": time.time(),
            "source_sha256": hashlib.sha256(Path(source_path).read_bytes()).hexdigest(),
        }
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('collector_runtime',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(state, separators=(",", ":")),),
            )


def caller_identity():
    frame = sys._getframe(2)
    shared_route = None
    while frame:
        filename = frame.f_code.co_filename.replace("\\", "/")
        match = PLUGIN_PATH_RE.search(filename)
        if match and match.group(1) != COLLECTOR_PLUGIN_ID:
            plugin_id = match.group(1)
            relative = match.group(2)
            route = f"{relative}:{frame.f_code.co_name}"
            return plugin_id[:128], route[:240], "plugin"
        if "/astrbot/core/agent/" in filename:
            shared_route = f"agent/{Path(filename).name}:{frame.f_code.co_name}"
        elif shared_route is None and "/astrbot/builtin_stars/" in filename:
            shared_route = f"builtin/{Path(filename).name}:{frame.f_code.co_name}"
        frame = frame.f_back
    if shared_route:
        return "shared_chat", shared_route[:240], "shared_chat"
    return "unattributed", "unknown", "runtime"


def provider_identity(provider, kwargs):
    config = getattr(provider, "provider_config", {}) or {}
    provider_id = str(config.get("id") or "unknown")[:160]
    requested_model = kwargs.get("model")
    if requested_model:
        model = str(requested_model)
    else:
        try:
            model = str(config.get("embedding_model") or provider.get_model() or "unknown")
        except Exception:
            model = "unknown"
    return provider_id, model[:160]


def response_usage(response):
    usage = getattr(response, "usage", None)
    return {
        "token_input_other": max(0, int(getattr(usage, "input_other", 0) or 0)),
        "token_input_cached": max(0, int(getattr(usage, "input_cached", 0) or 0)),
        "token_output": max(0, int(getattr(usage, "output", 0) or 0)),
    }


def request_usage(response):
    """直接读取供应商 usage，保留缓存命中；只取计量字段，不保存正文。"""
    def field(value, name, default=None):
        return value.get(name, default) if isinstance(value, dict) else getattr(
            value, name, default
        )

    def number(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    usage = field(response, "usage")
    if usage is None:
        return None
    # Embedding 没有输出 Token；只读取供应商计量，不用向量维度或文本长度推算。
    prompt = field(usage, "prompt_tokens")
    if prompt is None:
        prompt = field(usage, "input_tokens", field(usage, "total_tokens"))
    if prompt is None:
        return None
    prompt = number(prompt)
    details = field(usage, "prompt_tokens_details")
    cached = field(details, "cached_tokens")
    if cached is None:
        cached = field(usage, "prompt_cache_hit_tokens", 0)
    cached = min(prompt, number(cached))
    return {
        "token_input_other": prompt - cached,
        "token_input_cached": cached,
        "token_output": number(field(usage, "completion_tokens",
                                    field(usage, "output_tokens"))),
    }


class MeteredStream:
    """透明转交流式内容，在原始 usage 到达或流结束时更新计量记录。"""
    def __init__(self, upstream, plugin, state, request, standalone=False):
        self.upstream = upstream
        self.iterator = upstream.__aiter__()
        self.plugin = plugin
        self.state = state
        self.request = request
        self.standalone = standalone

    def __getattr__(self, name):
        return getattr(self.upstream, name)

    def __aiter__(self):
        return self

    async def __aenter__(self):
        await self.upstream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            return await self.upstream.__aexit__(exc_type, exc, traceback)
        finally:
            await self._finish(exc or GeneratorExit())

    async def __anext__(self):
        try:
            item = await self.iterator.__anext__()
        except StopAsyncIteration:
            await self._finish()
            raise
        except BaseException as exc:
            await self._finish(exc)
            raise
        if request_usage(item) is not None:
            await self.plugin._update_request(self.state, self.request, item)
        return item

    async def _finish(self, error=None):
        if self.request["status"] != "in_progress":
            return
        await self.plugin._update_request(
            self.state, self.request, error=error, finished=True
        )
        if self.standalone:
            await self.plugin._finish_call(self.state, error=error)

    async def close(self):
        try:
            return await self.upstream.close()
        finally:
            await self._finish(GeneratorExit())


@register(
    "astrbot_plugin_usage_telemetry",
    "Bot Manager",
    "统一模型和搜索计量、余额提醒及完整指令查询",
    "2.0.0",
)
class UsageTelemetryPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = UsageStore(DATABASE_PATH)
        self._watch_task = None
        self._balance_task = None
        self._providers = {}
        self._request_clients = {}
        self._embedding_providers = {}
        self._sync_clients = {}
        self._state_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._balance_state = self._load_balance_state()
        self._search_meter = None

    async def initialize(self):
        from .search_meter import SearchMeter
        self._search_meter = SearchMeter()
        self._search_meter.start()
        self._wrap_all()
        # 日志可能不进入 systemd journal，用数据库中的进程/源码标记确认就绪。
        await asyncio.to_thread(self.store.mark_ready, __file__)
        self._watch_task = asyncio.create_task(self._watch_providers())
        self._balance_task = asyncio.create_task(self._watch_balance())
        await self._save_command_catalog()
        logger.info("Bot Manager usage telemetry enabled")

    async def terminate(self):
        tasks = [task for task in (self._watch_task, self._balance_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for provider, originals in list(self._providers.values()):
            if getattr(provider, "_bot_manager_usage_owner", None) is self:
                provider.text_chat, provider.text_chat_stream = originals
                for name in (
                    "_bot_manager_usage_owner",
                    "_bot_manager_usage_original_text",
                    "_bot_manager_usage_original_stream",
                ):
                    try:
                        delattr(provider, name)
                    except AttributeError:
                        pass
        self._providers.clear()
        for resource, original in self._request_clients.values():
            if getattr(resource, "_bot_manager_usage_owner", None) is self:
                resource.create = original
                delattr(resource, "_bot_manager_usage_owner")
        self._request_clients.clear()
        for provider, originals in getattr(self, "_embedding_providers", {}).values():
            if getattr(provider, "_bot_manager_embedding_owner", None) is self:
                for name, original in originals.items():
                    setattr(provider, name, original)
                delattr(provider, "_bot_manager_embedding_owner")
        for resource, descriptor in getattr(self, "_sync_clients", {}).values():
            if getattr(resource, "_bot_manager_embedding_owner", None) is self:
                resource.call = descriptor
                delattr(resource, "_bot_manager_embedding_owner")
        getattr(self, "_embedding_providers", {}).clear()
        getattr(self, "_sync_clients", {}).clear()
        if getattr(self, "_search_meter", None) is not None:
            await asyncio.to_thread(self._search_meter.close)

    async def _watch_providers(self):
        while True:
            await asyncio.sleep(15)
            self._wrap_all()
            await self._save_command_catalog()

    async def _save_command_catalog(self):
        from .command_query import command_catalog, save_catalog
        try:
            catalog = command_catalog()
            runtime = {
                "pid": os.getpid(), "updatedAt": int(time.time()),
                "version": "2.0.0",
                "search": self._search_meter.status() if self._search_meter else {},
            }
            await asyncio.to_thread(save_catalog, CATALOG_PATH, catalog, runtime)
        except Exception as exc:
            logger.warning("Bot Manager: 指令目录刷新失败 (%s)", type(exc).__name__)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("botmanager", alias={"bm", "bot管理"})
    async def manager_command(self, event: AstrMessageEvent, arguments: GreedyStr):
        """统一管理入口：帮助、状态、余额、用量、搜索、指令、详情。"""
        if not event.is_admin():
            yield event.plain_result("权限不足：仅 AstrBot 管理员可用。")
            return
        from .command_query import HELP, command_catalog, command_details, list_commands
        from .usage_query import search_text, usage_text
        try:
            parts = shlex.split(str(arguments))
        except ValueError:
            parts = str(arguments).split()
        action = parts.pop(0).casefold() if parts else "帮助"
        try:
            if action in ("帮助", "help", "?"):
                text = HELP
            elif action in ("余额", "钱包", "账户余额", "balance"):
                text = await self._balance_text()
            elif action in ("状态", "status"):
                meter = self._search_meter.status() if self._search_meter else {}
                text = (
                    "Bot Manager 2.0.0 · 集成管理\n"
                    f"模型采集：{len(self._providers)} 个提供商；"
                    f"向量采集：{len(self._embedding_providers)} 个提供商\n"
                    f"搜索计量：{len(meter.get('tools', []))}/2 个工具\n"
                    f"本进程上报成功 {meter.get('accepted', 0)}；"
                    f"结果未知 {meter.get('unknown', 0)}；"
                    f"丢弃 {meter.get('dropped', 0)}；排队 {meter.get('queued', 0)}\n"
                    f"余额检查：{'运行中' if self._balance_task and not self._balance_task.done() else '未运行'}"
                    "（每5分钟；低于¥5提醒）\n"
                    f"已注册指令：{len(command_catalog())} 项（含停用插件）\n/bm 帮助 查看完整管理指令"
                )
            elif action in ("用量", "usage", "搜索", "search"):
                if len(parts) > 1:
                    raise ValueError("请只提供一个时间范围，例如：/bm 用量 今天")
                range_name = parts[0] if parts else "今天"
                if action in ("用量", "usage"):
                    text = await asyncio.to_thread(usage_text, DATABASE_PATH, range_name)
                else:
                    text = await asyncio.to_thread(search_text, range_name=range_name)
            elif action in ("指令", "命令", "查询", "commands", "详情", "detail"):
                page = 1
                if parts and re.fullmatch(r"-?\d+", parts[-1]):
                    page = int(parts.pop())
                keyword = " ".join(parts)
                catalog = command_catalog()
                if action in ("详情", "detail") and keyword:
                    text = command_details(catalog, keyword, page)
                else:
                    text = list_commands(catalog, keyword, page)
            else:
                text = f"未知管理指令「{action}」。\n{HELP}"
        except ValueError as exc:
            text = str(exc)
        except (sqlite3.Error, OSError):
            text = "暂时无法读取计量数据库，请查看 /bm 状态 或稍后重试。"
        yield event.plain_result(text)

    @staticmethod
    def _default_balance_state():
        return {
            "version": 1,
            "lastBalance": None,
            "lastCheckedAt": None,
            "lowActive": False,
            "rechargeThanked": False,
            "cycleId": None,
            "channels": {
                "ownerPrivate": None,
                "ownerGroup": None,
                "recentGroup": None,
            },
            "notifications": [],
        }

    @classmethod
    def _load_balance_state(cls):
        state = cls._default_balance_state()
        try:
            payload = json.loads(NOTIFICATION_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("version") == 1:
                state.update(payload)
                if isinstance(payload.get("channels"), dict):
                    state["channels"].update(payload["channels"])
                if not isinstance(state.get("notifications"), list):
                    state["notifications"] = []
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        changed = False
        for notification in state["notifications"]:
            targets = notification.get("targets") or {}
            for name, status in list(targets.items()):
                if status == "sending":
                    targets[name] = "unknown"
                    changed = True
        if changed:
            cls._save_balance_state(state)
        return state

    @staticmethod
    def _save_balance_state(state):
        NOTIFICATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = NOTIFICATION_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, NOTIFICATION_STATE_PATH)

    @staticmethod
    def _decimal(value):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return number if number.is_finite() else None

    @classmethod
    def _apply_balance_sample(cls, state, current_balance, checked_at):
        current = cls._decimal(current_balance)
        if current is None or current < 0:
            raise ValueError("invalid balance sample")
        previous = cls._decimal(state.get("lastBalance"))
        was_low_active = bool(state.get("lowActive"))
        actions = []
        if current < LOW_BALANCE_THRESHOLD and not was_low_active:
            cycle_id = uuid.uuid4().hex
            state["lowActive"] = True
            state["rechargeThanked"] = False
            state["cycleId"] = cycle_id
            state["notifications"].append(
                {
                    "id": f"low-{cycle_id}",
                    "kind": "low_balance",
                    "createdAt": checked_at,
                    "targets": {"owner": "pending"},
                }
            )
            actions.append("low_balance")

        increase = (
            current - previous
            if previous is not None and current > previous
            else Decimal("0")
        )
        if (
            was_low_active
            and not state.get("rechargeThanked")
            and increase >= MINIMUM_RECHARGE_INCREMENT
        ):
            cycle_id = state.get("cycleId") or uuid.uuid4().hex
            state["rechargeThanked"] = True
            state["notifications"].append(
                {
                    "id": f"recharge-{cycle_id}",
                    "kind": "recharge_thanks",
                    "createdAt": checked_at,
                    "targets": {"group": "pending", "private": "pending"},
                }
            )
            actions.append("recharge_thanks")

        if current >= LOW_BALANCE_THRESHOLD:
            state["lowActive"] = False
        state["lastBalance"] = format(current, "f")
        state["lastCheckedAt"] = checked_at
        if len(state["notifications"]) > 50:
            terminal = [
                item
                for item in state["notifications"]
                if all(
                    value in TERMINAL_NOTIFICATION_STATES
                    for value in (item.get("targets") or {}).values()
                )
            ]
            removable = {item.get("id") for item in terminal[:-20]}
            state["notifications"] = [
                item
                for item in state["notifications"]
                if item.get("id") not in removable
            ]
        return actions

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_notification_channels(self, event: AstrMessageEvent):
        now = int(time.time())
        changed = False
        async with self._state_lock:
            channels = self._balance_state["channels"]
            if event.get_group_id():
                channels["recentGroup"] = {
                    "umo": event.unified_msg_origin,
                    "seenAt": now,
                }
                changed = True
            if event.is_admin():
                key = "ownerPrivate" if event.is_private_chat() else "ownerGroup"
                channels[key] = {
                    "umo": event.unified_msg_origin,
                    "seenAt": now,
                }
                changed = True
            if changed:
                self._save_balance_state(self._balance_state)
        if changed:
            asyncio.create_task(self._dispatch_notifications())

    @staticmethod
    def _extract_cny_balance(payload):
        provider = payload.get("deepseek") or {}
        if provider.get("status") != "ok":
            raise RuntimeError(
                f"DeepSeek 余额状态异常：{provider.get('status') or 'unknown'}"
            )
        balances = provider.get("balances") or []
        preferred = next(
            (item for item in balances if item.get("currency") == "CNY"),
            None,
        )
        if not preferred:
            raise RuntimeError("DeepSeek 未返回人民币余额")
        total = UsageTelemetryPlugin._decimal(preferred.get("total"))
        if total is None:
            raise RuntimeError("DeepSeek 返回的人民币余额无效")
        return total

    async def _watch_balance(self):
        await asyncio.sleep(20)
        while True:
            try:
                payload = await asyncio.to_thread(self._query_deepseek_payload)
                current = self._extract_cny_balance(payload)
                checked_at = int(payload.get("timestamp") or time.time())
                async with self._state_lock:
                    actions = self._apply_balance_sample(
                        self._balance_state,
                        current,
                        checked_at,
                    )
                    self._save_balance_state(self._balance_state)
                if actions:
                    logger.info(
                        "Bot Manager balance transition: %s",
                        ", ".join(actions),
                    )
                await self._dispatch_notifications()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Unable to monitor DeepSeek balance: %s", exc)
            await asyncio.sleep(BALANCE_CHECK_SECONDS)

    def _wrap_all(self):
        providers = list(getattr(self.context.provider_manager, "provider_insts", []))
        for provider in providers:
            self._wrap_provider(provider)
            # 路由直接调用下游 _query，只包 SDK 请求，不能再新增一层逻辑调用。
            for upstream in getattr(provider, "upstreams", []) or []:
                client = getattr(upstream, "client", None)
                if client is not None:
                    self._wrap_request_client(client, routed=True)
        for provider in getattr(self.context.provider_manager, "embedding_provider_insts", []):
            self._wrap_embedding_provider(provider)

    def _wrap_embedding_provider(self, provider):
        if not hasattr(self, "_embedding_providers"):
            self._embedding_providers = {}
        if not hasattr(self, "_sync_clients"):
            self._sync_clients = {}
        self._wrap_request_client(provider, kind="embedding")
        if (id(provider) in self._embedding_providers
                or getattr(provider, "_bot_manager_embedding_owner", None) is not None):
            return
        originals = {}
        for name in ("get_embedding", "get_embeddings"):
            original = getattr(provider, name, None)
            if not callable(original):
                continue
            originals[name] = original

            def wrap(method):
                @functools.wraps(method)
                async def embedding_wrapper(*args, **kwargs):
                    if self._same_call(CALL_CONTEXT.get(), provider):
                        return await method(*args, **kwargs)
                    # 空批次没有发起 API，不记作供应商请求。
                    if args and not args[0]:
                        return await method(*args, **kwargs)
                    state = self._new_call(provider, kwargs, "embedding")
                    token = CALL_CONTEXT.set(state)
                    try:
                        await self._persist_call(state)
                        result = await method(*args, **kwargs)
                        await self._finish_call(state, response=result)
                        return result
                    except BaseException as exc:
                        await self._finish_call(state, error=exc)
                        raise
                    finally:
                        CALL_CONTEXT.reset(token)
                return embedding_wrapper
            setattr(provider, name, wrap(original))
        provider._bot_manager_embedding_owner = self
        self._embedding_providers[id(provider)] = (provider, originals)
        # DashScope 在工作线程返回 usage 后，官方适配器只返回向量；在丢弃前读取。
        if (getattr(provider, "provider_config", {}) or {}).get("type") == "dashscope_embedding":
            from dashscope import MultiModalEmbedding, TextEmbedding
            for resource in (TextEmbedding, MultiModalEmbedding):
                self._wrap_sync_embedding_client(resource)

    def _new_request(self, state, model, provider_id):
        request = {
            "id": str(uuid.uuid4()), "started_at": time.time(), "ended_at": None,
            "status": "in_progress", "usage_known": False,
            "model": str(model)[:160], "provider_id": str(provider_id)[:160],
            **response_usage(None),
        }
        state["request_details"].append(request)
        return request

    def _wrap_sync_embedding_client(self, resource):
        if getattr(resource, "_bot_manager_embedding_owner", None) is not None:
            return
        original = resource.call
        descriptor = resource.__dict__["call"]

        @functools.wraps(original)
        def call_wrapper(*args, **kwargs):
            state = CALL_CONTEXT.get()
            if not (state and state.get("_owner") is self
                    and state.get("call_kind") == "embedding"):
                return original(*args, **kwargs)
            loop = state["_loop"]
            # 只在 SDK 工作线程等待数据库写入；绝不在事件循环线程阻塞自身。
            try:
                if asyncio.get_running_loop() is loop:
                    return original(*args, **kwargs)
            except RuntimeError:
                pass
            async def begin():
                request = self._new_request(
                    state, kwargs.get("model") or state["model"], state["provider_id"])
                await self._persist_call(state)
                return request
            request = asyncio.run_coroutine_threadsafe(begin(), loop).result()
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                asyncio.run_coroutine_threadsafe(
                    self._update_request(state, request, error=exc, finished=True), loop
                ).result()
                raise
            # 外层取消后线程仍可能取得实际 usage，按原 call_id 回填，不能丢弃扣费用量。
            asyncio.run_coroutine_threadsafe(
                self._update_request(state, request, result, finished=True), loop
            ).result()
            return result
        resource.call = staticmethod(call_wrapper)
        resource._bot_manager_embedding_owner = self
        self._sync_clients[id(resource)] = (resource, descriptor)

    def _wrap_provider(self, provider):
        self._wrap_request_client(provider)
        key = id(provider)
        if key in self._providers:
            return
        if getattr(provider, "_bot_manager_usage_owner", None) is not None:
            return
        original_text = provider.text_chat
        original_stream = provider.text_chat_stream

        @functools.wraps(original_text)
        async def text_chat_wrapper(*args, **kwargs):
            parent = CALL_CONTEXT.get()
            # 只防止同一任务、同一 Provider 的委托调用重复计量。
            # 新任务继承上下文或另一个 Provider 都应建立独立记录。
            if self._same_call(parent, provider):
                return await original_text(*args, **kwargs)
            state = self._new_call(provider, kwargs)
            token = CALL_CONTEXT.set(state)
            try:
                await self._persist_call(state)
                response = await original_text(*args, **kwargs)
                await self._finish_call(state, response=response)
                return response
            except BaseException as exc:
                await self._finish_call(state, error=exc)
                raise
            finally:
                CALL_CONTEXT.reset(token)

        @functools.wraps(original_stream)
        async def text_chat_stream_wrapper(*args, **kwargs):
            parent = CALL_CONTEXT.get()
            if self._same_call(parent, provider):
                async for item in original_stream(*args, **kwargs):
                    yield item
                return
            state = self._new_call(provider, kwargs)
            iterator = original_stream(*args, **kwargs)
            response = None
            error = None
            try:
                await self._persist_call(state)
                while True:
                    # 每次读取时设置上下文，yield 给业务代码前恢复；
                    # 同一个流由不同任务读取/关闭也不会残留标记。
                    state["_task"] = asyncio.current_task()
                    token = CALL_CONTEXT.set(state)
                    try:
                        item = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        CALL_CONTEXT.reset(token)
                    if state["ttft_ms"] is None:
                        state["ttft_ms"] = max(
                            0, round((time.time() - state["started_at"]) * 1000)
                        )
                    if getattr(item, "usage", None) is not None:
                        response = item
                    yield item
            except BaseException as exc:
                error = exc
                raise
            finally:
                try:
                    await iterator.aclose()
                finally:
                    await self._finish_call(state, response=response, error=error)

        provider._bot_manager_usage_owner = self
        provider._bot_manager_usage_original_text = original_text
        provider._bot_manager_usage_original_stream = original_stream
        provider.text_chat = text_chat_wrapper
        provider.text_chat_stream = text_chat_stream_wrapper
        self._providers[key] = (provider, (original_text, original_stream))

    def _same_call(self, state, provider):
        return (
            state is not None and state.get("_owner") is self
            and state.get("_active") and state["_provider_key"] == id(provider)
            and state["_task"] is asyncio.current_task()
        )

    def _new_call(self, provider, kwargs, kind="llm"):
        plugin_id, route, source = caller_identity()
        provider_id, model = provider_identity(provider, kwargs)
        started = time.time()
        return {
            "call_id": str(uuid.uuid4()), "started_at": started, "ended_at": started,
            "plugin_id": plugin_id, "route": route, "source": source,
            "provider_id": provider_id, "model": model,
            "call_kind": kind,
            **response_usage(None), "status": "in_progress", "latency_ms": 0,
            "ttft_ms": None, "error_type": None, "attempts": 0,
            "usage_known": False, "request_details": [],
            "_owner": self, "_provider_key": id(provider),
            "_task": asyncio.current_task(), "_active": True,
            "_write_lock": asyncio.Lock(),
            "_loop": asyncio.get_running_loop(),
        }

    async def _persist_call(self, state):
        async with state["_write_lock"]:
            requests = state["request_details"]
            if requests:
                for field in ("token_input_other", "token_input_cached", "token_output"):
                    state[field] = sum(item.get(field, 0) for item in requests)
                state["usage_known"] = all(item["usage_known"] for item in requests)
                state["attempts"] = len(requests)
            state["ended_at"] = time.time()
            state["latency_ms"] = max(
                0, round((state["ended_at"] - state["started_at"]) * 1000)
            )
            event = {key: value for key, value in state.items() if not key.startswith("_")}
            event["request_details"] = [dict(item) for item in requests]
            try:
                await asyncio.to_thread(self.store.record, event)
            except Exception:
                logger.warning("Unable to record Bot Manager usage event", exc_info=True)

    @staticmethod
    def _error_status(error):
        return "cancelled" if isinstance(
            error, (asyncio.CancelledError, GeneratorExit)
        ) else "error"

    async def _finish_call(self, state, response=None, error=None):
        if not state["_active"]:
            return
        state["_active"] = False
        if error is not None:
            state["status"] = self._error_status(error)
            state["error_type"] = type(error).__name__
        elif response is not None and getattr(response, "role", "") == "err":
            state["status"] = "error"
        elif state["request_details"]:
            state["status"] = "completed" if all(
                item["status"] == "completed" for item in state["request_details"]
            ) else "incomplete"
            # 已失败的重试不改变最终调用成功状态，但其未知费用仍保留。
            if response is not None:
                state["status"] = "completed"
        else:
            state["status"] = "completed" if response is not None else "incomplete"
        for request in state["request_details"]:
            if request["status"] == "in_progress":
                request["status"] = (
                    self._error_status(error) if error is not None else "incomplete"
                )
                request["ended_at"] = time.time()
        if not state["request_details"]:
            state.update(response_usage(response))
            state["usage_known"] = any(
                state[field] for field in
                ("token_input_other", "token_input_cached", "token_output")
            )
            state["attempts"] = 1
        await self._persist_call(state)

    async def _update_request(self, state, request, response=None, error=None,
                              finished=False):
        usage = request_usage(response)
        if usage is not None:
            # 流式 usage 是累计值，同一次请求覆盖而不是逐块累加。
            request.update(usage)
            request["usage_known"] = True
        if response is not None:
            field = lambda name: response.get(name) if isinstance(response, dict) else getattr(response, name, None)
            response_id = field("id")
            request_id = field("_request_id") or field("request_id")
            if isinstance(response_id, str):
                request["response_id"] = response_id[:240]
            if isinstance(request_id, str):
                request["provider_request_id"] = request_id[:240]
        if finished:
            request["ended_at"] = time.time()
            request["status"] = (
                self._error_status(error) if error is not None
                else "completed" if request["usage_known"] else "incomplete"
            )
            request["error_type"] = type(error).__name__ if error is not None else None
            if error is not None:
                # 中断时已收到的累计用量只是已知部分，后续实际扣费仍待对账。
                request["usage_known"] = False
        await self._persist_call(state)

    def _wrap_request_client(self, provider, kind="llm", routed=False):
        resource = getattr(getattr(provider, "client", None), "embeddings", None) if kind == "embedding" else getattr(
            getattr(getattr(provider, "client", None), "chat", None),
            "completions", None,
        )
        if resource is None or not callable(getattr(resource, "create", None)):
            return
        key = id(resource)
        if key in self._request_clients:
            return
        if getattr(resource, "_bot_manager_usage_owner", None) is not None:
            return
        original = resource.create

        @functools.wraps(original)
        async def create_wrapper(*args, **kwargs):
            state = CALL_CONTEXT.get()
            standalone = not (
                state is not None and state.get("_owner") is self
                and state.get("_active")
                and (routed or state["_task"] is asyncio.current_task())
            )
            if standalone:
                state = self._new_call(provider, kwargs, kind)
            provider_id, model = provider_identity(provider, kwargs)
            request = self._new_request(
                state, model, state["provider_id"] if routed else provider_id)
            if routed:
                request["upstream_provider_id"] = provider_id
            try:
                # 发起调用前落库，即使进程重启也能看见已开始的尝试。
                await self._persist_call(state)
                result = await original(*args, **kwargs)
                if kwargs.get("stream"):
                    headers = getattr(getattr(result, "response", None), "headers", None) or {}
                    request_id = headers.get("x-request-id") or headers.get("request-id")
                    if isinstance(request_id, str):
                        request["provider_request_id"] = request_id[:240]
                        await self._persist_call(state)
                    return MeteredStream(result, self, state, request, standalone)
                await self._update_request(state, request, result, finished=True)
                if standalone:
                    await self._finish_call(state, response=result)
                return result
            except BaseException as exc:
                await self._update_request(state, request, error=exc, finished=True)
                if standalone:
                    await self._finish_call(state, error=exc)
                raise

        resource.create = create_wrapper
        resource._bot_manager_usage_owner = self
        self._request_clients[key] = (resource, original)

    @staticmethod
    def _query_deepseek_payload():
        token = INTERNAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise RuntimeError("Bot Manager 内部令牌无效")
        request = urllib.request.Request(
            BALANCE_ENDPOINT,
            headers={
                "Accept": "application/json",
                "X-Bot-Manager-Token": token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read(256 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"余额服务返回 HTTP {exc.code}") from exc
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "余额服务查询失败"))
        return payload

    @classmethod
    def _query_deepseek_balance(cls):
        payload = cls._query_deepseek_payload()
        provider = payload.get("deepseek") or {}
        if provider.get("status") != "ok":
            status = provider.get("status") or "unknown"
            raise RuntimeError(f"DeepSeek 余额状态异常：{status}")
        balances = provider.get("balances") or []
        preferred = next(
            (item for item in balances if item.get("currency") == "CNY"),
            balances[0] if balances else None,
        )
        if not preferred:
            raise RuntimeError("DeepSeek 未返回余额明细")
        timestamp = int(payload.get("timestamp") or time.time())
        checked_at = datetime.fromtimestamp(timestamp, REPORT_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        available = "可用" if provider.get("available") is not False else "余额不足"
        return (
            f"DeepSeek 当前余额：{preferred.get('currency', '')} "
            f"{preferred.get('total', '0')}；充值余额 "
            f"{preferred.get('toppedUp', '0')}；赠送余额 "
            f"{preferred.get('granted', '0')}；账户状态：{available}；"
            f"查询时间（北京时间）：{checked_at}。"
        )

    def _claim_notifications(self):
        claims = []
        channels = self._balance_state["channels"]
        for notification in self._balance_state["notifications"]:
            targets = notification.get("targets") or {}
            kind = notification.get("kind")
            for target, status in targets.items():
                if status != "pending":
                    continue
                channel = None
                if kind == "low_balance" and target == "owner":
                    channel = channels.get("ownerPrivate") or channels.get("ownerGroup")
                elif kind == "recharge_thanks" and target == "group":
                    channel = channels.get("recentGroup")
                elif kind == "recharge_thanks" and target == "private":
                    channel = channels.get("ownerPrivate")
                if not channel or not channel.get("umo"):
                    continue
                targets[target] = "sending"
                claims.append(
                    {
                        "id": notification["id"],
                        "kind": kind,
                        "target": target,
                        "umo": channel["umo"],
                    }
                )
        if claims:
            self._save_balance_state(self._balance_state)
        return claims

    async def _finish_notification(self, claim, status):
        async with self._state_lock:
            for notification in self._balance_state["notifications"]:
                if notification.get("id") == claim["id"]:
                    notification["targets"][claim["target"]] = status
                    notification["updatedAt"] = int(time.time())
                    if all(
                        value in TERMINAL_NOTIFICATION_STATES
                        for value in notification["targets"].values()
                    ):
                        notification.pop("messageBase", None)
                    self._save_balance_state(self._balance_state)
                    break

    async def _persona_notification_base(self, claim):
        fallbacks = {
            "low_balance": "主人，钱包快要见底啦……再不添一点，我可要抱着空饭碗发呆了。",
            "recharge_thanks": "谢谢主人给我添粮，我收到这份心意啦，会珍惜着用的。",
        }
        prompts = {
            "low_balance": (
                "你正在主动联系主人。模型钱包余额已经低于5元，请严格保持既定人设，"
                "用自然、可爱但不夸张的1到2句话请求主人充值。不要输出标题、解释、"
                "Markdown或具体余额数字，只输出要发送的话。"
            ),
            "recharge_thanks": (
                "主人刚为模型钱包充值。请严格保持既定人设，写一条可同时复用于"
                "群聊和私聊的渠道中性感谢主文案，1到2句话、自然真诚。不要出现"
                "“群里”“私下”等渠道词，不要提具体金额、余额、API、系统或后台，"
                "不要输出标题、解释或Markdown，只输出主文案。"
            ),
        }
        prompt_key = claim["kind"]
        try:
            provider = await self.context.get_using_provider_async(umo=claim["umo"])
            if provider is None:
                raise RuntimeError("no provider for notification channel")
            persona = await self.context.persona_manager.get_default_persona_v3(
                claim["umo"]
            )
            system_prompt = str((persona or {}).get("prompt") or "")
            response = await provider.text_chat(
                prompt=prompts[prompt_key],
                system_prompt=system_prompt,
                request_max_retries=1,
                max_tokens=100,
            )
            text = str(getattr(response, "completion_text", "") or "").strip()
            text = text.strip("\"'“”‘’").replace("\n", " ")
            text = re.sub(r"\s+", " ", text)[:180].strip()
            if not text or (
                prompt_key == "recharge_thanks"
                and re.search(r"(?:[¥￥]\s*\d|\d+(?:\.\d+)?\s*元)", text)
            ):
                raise RuntimeError("generated notification text is unsuitable")
            return text
        except Exception as exc:
            logger.warning("Unable to generate persona balance notification: %s", exc)
            return fallbacks[prompt_key]

    async def _notification_message_base(self, claim):
        async with self._state_lock:
            for notification in self._balance_state["notifications"]:
                if notification.get("id") == claim["id"]:
                    cached = str(notification.get("messageBase") or "").strip()
                    if cached:
                        return cached
                    break
        generated = await self._persona_notification_base(claim)
        async with self._state_lock:
            for notification in self._balance_state["notifications"]:
                if notification.get("id") != claim["id"]:
                    continue
                cached = str(notification.get("messageBase") or "").strip()
                if cached:
                    return cached
                notification["messageBase"] = generated
                notification["updatedAt"] = int(time.time())
                self._save_balance_state(self._balance_state)
                return generated
        return generated

    @staticmethod
    def _decorate_notification_text(claim, message_base):
        if claim["kind"] != "recharge_thanks":
            return message_base
        if claim["target"] == "group":
            return f"当着大家的面也要说一句：{message_base}"
        return f"私下再认真说一句：{message_base}"

    async def _dispatch_notifications(self):
        async with self._dispatch_lock:
            async with self._state_lock:
                claims = self._claim_notifications()
            grouped_claims = {}
            for claim in claims:
                grouped_claims.setdefault(claim["id"], []).append(claim)
            for event_claims in grouped_claims.values():
                preferred = next(
                    (
                        claim
                        for claim in event_claims
                        if claim["target"] == "private"
                    ),
                    event_claims[0],
                )
                message_base = await self._notification_message_base(preferred)
                for claim in event_claims:
                    try:
                        text = self._decorate_notification_text(
                            claim,
                            message_base,
                        )
                        sent = await self.context.send_message(
                            claim["umo"],
                            MessageChain().message(text),
                        )
                        if not sent:
                            raise RuntimeError("notification platform is unavailable")
                    except asyncio.CancelledError:
                        await self._finish_notification(claim, "unknown")
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Unable to send balance notification: %s",
                            exc,
                        )
                        await self._finish_notification(claim, "unknown")
                    else:
                        await self._finish_notification(claim, "sent")

    async def _balance_text(self):
        try:
            return await asyncio.to_thread(self._query_deepseek_balance)
        except Exception as exc:
            logger.warning("Unable to query DeepSeek balance: %s", exc)
            return f"暂时无法获取 DeepSeek 当前余额：{str(exc)[:160]}"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("账户余额", alias={"余额", "钱包"})
    async def balance_command(self, event: AstrMessageEvent):
        """查询 DeepSeek 当前账户余额。"""
        yield event.plain_result(await self._balance_text())

    @filter.llm_tool(name="get_wallet_balance")
    async def get_wallet_balance(
        self,
        event: AstrMessageEvent,
        refresh: bool = True,
    ) -> str:
        """查询 DeepSeek API 账户当前余额。仅管理员可用；需要回答余额时必须调用本工具，不得猜测。

        Args:
            refresh(boolean): 是否立即刷新余额，通常传 true
        """
        del refresh
        if not event.is_admin():
            return "权限不足：只有 Bot 管理员可以查询账户余额。"
        return await self._balance_text()

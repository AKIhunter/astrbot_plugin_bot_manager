"""搜索计量随主插件启停，避免旧版模块导入补丁在热重载后残留。"""

import functools
import json
import queue
import threading
import time
import urllib.request
from pathlib import Path

from astrbot.api import logger
from .runtime_paths import TOKEN_PATH, SERVICE_BASE

ENDPOINT = SERVICE_BASE + "/api/internal/search-event"
WRAP_FLAG = "_bot_manager_search_meter"
TOOL_KINDS = {
    "DeepSeekWebSearchTool": "search",
    "DeepSeekFetchWebPageTool": "fetch",
}


def billed(result):
    if not isinstance(result, str) or not result.startswith("Error:"):
        return True
    return "does not return any" in result


class SearchMeter:
    def __init__(self):
        self.queue = queue.Queue(maxsize=512)
        self.originals = {}
        self.thread = None
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.accepted = self.failed = self.dropped = 0

    def start(self, tools=None):
        if tools is None:
            try:
                from astrbot.core.tools import web_search_tools
                tools = web_search_tools
            except ImportError:
                logger.warning("Bot Manager: 搜索工具不可用，跳过搜索计量")
                return
        for name, kind in TOOL_KINDS.items():
            cls = getattr(tools, name, None)
            if cls is None or cls in self.originals:
                continue
            original = cls.__dict__.get("call")
            if original is None:
                continue
            if getattr(original, WRAP_FLAG, False):
                logger.warning("Bot Manager: %s 已有搜索计量补丁，跳过重复包装", name)
                continue
            wrapper = self._wrapper(original, kind)
            cls.call = wrapper
            self.originals[cls] = (original, wrapper)

    def _wrapper(self, original, kind):
        @functools.wraps(original)
        async def call(tool, context, **kwargs):
            result = await original(tool, context, **kwargs)
            if billed(result):
                self.report(kind)
            return result

        setattr(call, WRAP_FLAG, True)
        return call

    def report(self, kind):
        if self.stopping.is_set():
            self.dropped += 1
            return
        try:
            self.queue.put_nowait({"kind": kind, "at": int(time.time())})
        except queue.Full:
            self.dropped += 1
            logger.warning("Bot Manager: 搜索计量队列已满")
            return
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._worker, name="bot-manager-search-meter", daemon=True
                )
                self.thread.start()

    def _post(self, event):
        # 每次读取当前令牌，服务端轮换令牌后无需重启插件。
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("内部计量令牌为空")
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Bot-Manager-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
        if not payload.get("ok") or payload.get("accepted") != 1:
            raise ValueError("搜索计量接口未确认流水")

    def _worker(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._post(event)
                self.accepted += 1
            except Exception as exc:
                # 不盲目重试：请求已被接受但响应丢失时，重试可能重复计费。
                self.failed += 1
                logger.warning("Bot Manager: 搜索流水上报结果未知 (%s)", type(exc).__name__)
            finally:
                self.queue.task_done()

    def close(self):
        for cls, (original, wrapper) in self.originals.items():
            if cls.call is wrapper:
                cls.call = original
        self.originals.clear()
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Bot Manager: 搜索计量仍有待处理流水")

    def status(self):
        return {
            "tools": sorted(cls.__name__ for cls in self.originals),
            "queued": self.queue.qsize(),
            "accepted": self.accepted,
            "unknown": self.failed,
            "dropped": self.dropped,
        }

"""只读查询既有流水；不改历史数据，不将缺少用量的调用当作免费。"""

import sqlite3
import json
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from .runtime_paths import TOKEN_PATH, SERVICE_BASE

SEARCH_USAGE_ENDPOINT = SERVICE_BASE + "/api/internal/search-usage"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def period(name="今天", now=None):
    end = time.time() if now is None else now
    midnight = datetime.fromtimestamp(end, TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if name in ("今天", "今日", "today"):
        start = midnight.timestamp()
    elif name in ("昨天", "昨日", "yesterday"):
        start, end = (midnight - timedelta(days=1)).timestamp(), midnight.timestamp()
    elif name in ("7天", "7d", "30天", "30d"):
        start = (midnight - timedelta(days=int(name[:-1]) - 1)).timestamp()
    elif name in ("全部", "all"):
        start = 0
    else:
        raise ValueError("时间范围支持：今天、昨天、7天、30天、全部")
    return start, end


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def usage_text(path, range_name="今天"):
    start, end = period(range_name)
    with closing(readonly(path)) as connection:
        row = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(token_input_other),0),"
            "COALESCE(SUM(token_input_cached),0),COALESCE(SUM(token_output),0),"
            "COALESCE(SUM(CASE WHEN usage_known IS NULL OR usage_known=0 THEN 1 ELSE 0 END),0) "
            "FROM usage_events WHERE started_at>=? AND started_at<?", (start, end)
        ).fetchone()
        kinds = connection.execute(
            "SELECT call_kind,COUNT(*) FROM usage_events "
            "WHERE started_at>=? AND started_at<? GROUP BY call_kind", (start, end)
        ).fetchall()
        plugins = connection.execute(
            "SELECT plugin_id,COUNT(*),SUM(token_input_other+token_input_cached+token_output) "
            "FROM usage_events WHERE started_at>=? AND started_at<? GROUP BY plugin_id "
            "ORDER BY 3 DESC,1 LIMIT 10", (start, end)
        ).fetchall()
    count, other, cached, output, unknown = row
    lines = [
        f"Bot Manager 用量 · {range_name}（北京时间）",
        f"采集调用 {count:,} 次；" + "、".join(f"{kind} {n:,}" for kind, n in kinds),
        f"已记录 Token：{other + cached + output:,}",
        f"输入 {other:,}；缓存输入 {cached:,}；输出 {output:,}",
        f"用量未知 {unknown:,} 次（不能当作零费用）",
        "插件排行（前10）：",
    ]
    lines.extend(f"{name}：{n:,} 次 / {tokens or 0:,} Token" for name, n, tokens in plugins)
    lines.append("采集调用数不等于上游请求数；重试明细和模型费用请在集成平台查看。")
    return "\n".join(lines)


def search_text(path=None, range_name="今天"):
    start, end = period(range_name)
    if path is None:
        # 不直接打开 root 的 WAL 库；只读模式仍可能需要创建共享内存文件。
        request = urllib.request.Request(
            f"{SEARCH_USAGE_ENDPOINT}?since={int(start)}&until={int(end) - int(end == int(end))}",
            headers={"X-Bot-Manager-Token": TOKEN_PATH.read_text(encoding="utf-8").strip()},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            counts = json.load(response)
        if not counts.get("ok"):
            raise ValueError("搜索统计接口暂时不可用")
    else:
        # 测试及有库权限的离线核对可显式传入数据库。
        with closing(readonly(path)) as connection:
            counts = dict(connection.execute(
                "SELECT kind,COUNT(*) FROM search_events WHERE timestamp>=? AND timestamp<? "
                "GROUP BY kind", (start, end)
            ).fetchall())
    search, fetch = counts.get("search", 0), counts.get("fetch", 0)
    amount = Decimal(search + fetch) * Decimal("0.005")
    return (
        f"DeepSeek 搜索计量 · {range_name}（北京时间）\n"
        f"搜索 {search:,} 次；网页抓取 {fetch:,} 次；共 {search + fetch:,} 次\n"
        f"计量费用 ¥{amount:.3f}（¥0.005/成功请求；不消耗 Token）\n"
        "按平台已收到流水统计；失败或丢失的上报需与官方账单核对。"
    )

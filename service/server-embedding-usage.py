#!/usr/bin/env python3
"""Recover and incrementally import registered embedding gateway counters."""

import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
CHECKPOINT_KEY = "embedding_gateway_checkpoint_v1"
COUNTERS = ("requests", "inputs", "tokens", "unknown_requests")


def initialize(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS usage_reconciliations (
            call_id TEXT PRIMARY KEY, record_type TEXT NOT NULL,
            note TEXT NOT NULL, amount_cny TEXT,
            period_start REAL NOT NULL, period_end REAL NOT NULL,
            evidence_json TEXT NOT NULL
        )
    """)


def read_counter(cache_path, namespace):
    with closing(sqlite3.connect(Path(cache_path).resolve().as_uri() + "?mode=ro",
                                uri=True, timeout=5)) as connection:
        row = connection.execute(
            "SELECT requests,inputs,tokens,unknown_requests FROM usage_totals "
            "WHERE namespace=?", (namespace,)
        ).fetchone()
    if row is None:
        raise ValueError("registered gateway namespace is missing")
    counter = dict(zip(COUNTERS, row))
    if any(type(value) is not int or value < 0 for value in counter.values()):
        raise ValueError("invalid gateway counters")
    return counter


def insert_record(connection, checkpoint, call_id, record_type, tokens,
                  period_start, period_end, note, evidence, amount=None):
    """汇总流水不冒充逐笔调用；另表保留来源、真实覆盖区间和官方金额。"""
    connection.execute("""
        INSERT INTO usage_events
            (call_id,started_at,ended_at,plugin_id,route,source,provider_id,model,
             token_input_other,token_input_cached,token_output,status,latency_ms,
             ttft_ms,error_type,attempts,usage_known,request_details,call_kind)
        VALUES (?,?,?,'unattributed',?,?,?,?,?,0,0,'completed',0,NULL,NULL,0,1,'[]','embedding')
    """, (call_id, period_start, period_end, "embedding/aggregate", record_type,
          checkpoint["provider_id"], checkpoint["model"], tokens))
    connection.execute(
        "INSERT INTO usage_reconciliations VALUES (?,?,?,?,?,?,?)",
        (call_id, record_type, note, amount, period_start, period_end,
         json.dumps(evidence, ensure_ascii=False, sort_keys=True)),
    )


def recover(connection, plan):
    """同一事务写补录与检查点；再次执行同一凭据不会重复入账。"""
    old = connection.execute("SELECT value FROM metadata WHERE key=?",
                             (CHECKPOINT_KEY,)).fetchone()
    if old:
        checkpoint = json.loads(old[0])
        if checkpoint["recovery_id"] != plan["recovery_id"]:
            raise ValueError("a different gateway recovery is already registered")
        return {"ok": True, "alreadyApplied": True}
    counter = plan["counter"]
    if any(type(counter.get(key)) is not int or counter[key] < 0 for key in COUNTERS):
        raise ValueError("invalid recovery counters")
    if counter["unknown_requests"]:
        raise ValueError("cannot reconcile unknown gateway usage")
    delta = plan["official_tokens"] - plan["platform_tokens"] - counter["tokens"]
    amount = (Decimal(plan["official_amount_cny"])
              - Decimal(plan["platform_amount_cny"])
              - Decimal(counter["tokens"]) * Decimal(plan["rate_cny_per_million"]) / 1000000)
    if delta < 0 or amount < 0 or not amount.is_finite():
        raise ValueError("official snapshot is below measured usage")
    checkpoint = {key: plan[key] for key in
                  ("recovery_id", "namespace", "model", "provider_id")}
    checkpoint.update(counter=counter, observed_at=plan["observed_at"])
    initialize(connection)
    start, end = plan["period_start"], plan["observed_at"]
    if start > end or datetime.fromtimestamp(start, TIMEZONE).date() != datetime.fromtimestamp(end, TIMEZONE).date():
        raise ValueError("recovery must cover one confirmed billing day")
    evidence = dict(plan)
    insert_record(connection, checkpoint, plan["recovery_id"] + ":gateway",
                  "embedding_gateway_recovery", counter["tokens"], start, end,
                  "网关持久化累计用量恢复；无逐笔时间，不计入小时分布。",
                  evidence)
    if delta or amount:
        insert_record(connection, checkpoint, plan["recovery_id"] + ":official",
                      "official_billing_adjustment", delta, start, end,
                      "按用户确认的今日百炼累计账单补齐差额；官方快照截至时间未提供，"
                      "不推断逐笔请求、插件或小时。", evidence, format(amount, "f"))
    connection.execute("INSERT INTO metadata(key,value) VALUES (?,?)",
                       (CHECKPOINT_KEY, json.dumps(checkpoint, sort_keys=True)))
    return {"ok": True, "gatewayTokens": counter["tokens"],
            "adjustmentTokens": delta, "adjustmentAmountCny": format(amount, "f")}


def _sync_legacy(connection, cache_path, now=None, frozen=None):
    now = time.time() if now is None else now
    row = connection.execute("SELECT value FROM metadata WHERE key=?",
                             (CHECKPOINT_KEY,)).fetchone()
    # 不猜模型、币种或初始累计；只有已经审核登记的命名空间才能自动增量采集。
    if not row:
        return {"ok": True, "registered": False}
    checkpoint = json.loads(row[0])
    current = frozen if frozen is not None else read_counter(cache_path, checkpoint["namespace"])
    previous = checkpoint["counter"]
    delta = {key: current[key] - previous[key] for key in COUNTERS}
    if now < checkpoint["observed_at"] or any(value < 0 for value in delta.values()):
        raise ValueError("gateway counters or clock moved backwards; reconciliation required")
    if delta["unknown_requests"]:
        raise ValueError("gateway returned unknown usage; reconciliation required")
    old_day = datetime.fromtimestamp(checkpoint["observed_at"], TIMEZONE).date()
    day = datetime.fromtimestamp(now, TIMEZONE).date()
    if day != old_day and delta["tokens"]:
        # 累计值没有逐笔时间，跨午夜的未采集增量不能强行算到任一自然日。
        raise ValueError("uncollected usage spans billing days; reconciliation required")
    if delta["tokens"] and connection.execute(
        "SELECT 1 FROM usage_events WHERE provider_id=? AND model=? "
        "AND started_at>=? AND token_input_other+token_input_cached+token_output>0 "
        "AND source NOT IN ('embedding_gateway_recovery','official_billing_adjustment',"
        "'embedding_gateway_increment') LIMIT 1",
        (checkpoint["provider_id"], checkpoint["model"], checkpoint["observed_at"])
    ).fetchone():
        # SDK 若恢复返回 Token，先核对是否与网关同一批请求，避免两处计量重复相加。
        raise ValueError("SDK has newly measured embedding usage; overlap reconciliation required")
    initialize(connection)
    if delta["tokens"]:
        call_id = checkpoint["recovery_id"] + ":increment:" + day.isoformat()
        receipt = connection.execute(
            "SELECT evidence_json FROM usage_reconciliations WHERE call_id=?", (call_id,)
        ).fetchone()
        evidence = {"namespace": checkpoint["namespace"], "counter": current}
        if receipt:
            event = connection.execute(
                "SELECT source FROM usage_events WHERE call_id=?", (call_id,)
            ).fetchone()
            if not event or event[0] != "embedding_gateway_increment":
                raise ValueError("gateway import receipt has no matching event")
            connection.execute(
                "UPDATE usage_events SET token_input_other=token_input_other+?,ended_at=? "
                "WHERE call_id=?", (delta["tokens"], now, call_id))
            connection.execute(
                "UPDATE usage_reconciliations SET period_end=?,evidence_json=? WHERE call_id=?",
                (now, json.dumps(evidence, sort_keys=True), call_id))
        else:
            insert_record(connection, checkpoint, call_id, "embedding_gateway_increment",
                          delta["tokens"], checkpoint["observed_at"], now,
                          "网关累计计量的新增用量；检查点防重复，无逐笔时间，不计入小时分布。",
                          evidence)
    checkpoint.update(counter=current, observed_at=now)
    connection.execute("UPDATE metadata SET value=? WHERE key=?",
                       (json.dumps(checkpoint, sort_keys=True), CHECKPOINT_KEY))
    return {"ok": True, "registered": True, "addedTokens": delta["tokens"],
            "counterTokens": current["tokens"]}

JOURNAL_KEY = "embedding_gateway_journal_v1"
HEALTH_KEY = "embedding_gateway_health_v1"


def sync(connection, cache_path, now=None):
    """用启动基线封存旧累计，再按流水 ID 重放；事务提交前不推进检查点。"""
    now = time.time() if now is None else now
    registered = connection.execute(
        "SELECT value FROM metadata WHERE key=?", (CHECKPOINT_KEY,)
    ).fetchone()
    if not registered:
        return {"ok": True, "registered": False}
    checkpoint = json.loads(registered[0])
    with closing(sqlite3.connect(Path(cache_path).resolve().as_uri() + "?mode=ro",
                                uri=True, timeout=5)) as cache:
        cache.row_factory = sqlite3.Row
        cache.execute("BEGIN")
        has_journal = cache.execute(
            "SELECT 1 FROM sqlite_master WHERE name='usage_journal_activation'"
        ).fetchone()
        activation = cache.execute(
            "SELECT * FROM usage_journal_activation WHERE namespace=?",
            (checkpoint["namespace"],)
        ).fetchone() if has_journal else None
        if activation is None:
            result = _sync_legacy(connection, cache_path, now)
            save_health(connection, dict(ok=True, mode="legacy", lastSyncAt=now))
            return result
        state_row = connection.execute(
            "SELECT value FROM metadata WHERE key=?", (JOURNAL_KEY,)
        ).fetchone()
        state = json.loads(state_row[0]) if state_row else {}
        if state and state["namespace"] != checkpoint["namespace"]:
            raise ValueError("journal namespace changed; reconciliation required")
        legacy = {}
        if not state:
            legacy = _sync_legacy(connection, cache_path, activation["activated_at"],
                                  json.loads(activation["counter_json"]))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS usage_embedding_measurements ("
            "call_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        changed = cache.execute(
            "SELECT rowid AS sequence,* FROM usage_journal WHERE namespace=? "
            "AND (rowid>? OR ended_at>=?) ORDER BY rowid",
            (checkpoint["namespace"], state.get("cursor", 0), state.get("observed_at", 0)),
        ).fetchall()
        traced = set()
        for row in changed:
            item = dict(row)
            trace = json.loads(item["trace_json"])
            if trace.get("call_id"):
                traced.add(trace["call_id"])
            elif item["kind"] == "upstream":
                # 无原调用身份的 HTTP 客户端只导入真实上游，不按自报 caller 猜插件。
                publish_gateway_event(connection, item, checkpoint)
        for call_id in traced:
            rows = cache.execute(
                "SELECT * FROM usage_journal WHERE namespace=? "
                "AND json_extract(trace_json,'$.call_id')=? ORDER BY started_at,id",
                (checkpoint["namespace"], call_id),
            ).fetchall()
            accesses = [dict(r) for r in rows if r["kind"] == "access"]
            upstreams = [dict(r) for r in rows if r["kind"] == "upstream"]
            requests = [journal_request(r, checkpoint) for r in upstreams]
            expected = sum(json.loads(r["info_json"]).get("misses", 0) for r in accesses)
            counted = sum(r["inputs"] for r in upstreams)
            known = bool(accesses) and all(r["tokens"] is not None for r in upstreams)
            known = known and counted >= expected
            # 原调用取消后，独立批次的真实用量仍归回原 call_id。
            payload = dict(
                token_input_other=sum(r["tokens"] or 0 for r in upstreams),
                token_input_cached=0, token_output=0, usage_known=known,
                attempts=len(upstreams), request_details=requests,
                measurement_source="livingmemory_journal",
                cache_info={key: sum(json.loads(r["info_json"]).get(key, 0) for r in accesses)
                            for key in ("hits", "misses", "coalesced")},
            )
            connection.execute(
                "INSERT INTO usage_embedding_measurements VALUES (?,?,?) "
                "ON CONFLICT(call_id) DO UPDATE SET payload_json=excluded.payload_json,"
                "updated_at=excluded.updated_at",
                (call_id, json.dumps(payload), now),
            )
        unknown = cache.execute(
            "SELECT count(*) FROM usage_journal WHERE namespace=? AND kind='upstream' "
            "AND tokens IS NULL AND status!='in_progress'", (checkpoint["namespace"],)
        ).fetchone()[0]
        pending = cache.execute(
            "SELECT count(*),min(started_at) FROM usage_journal WHERE namespace=? "
            "AND kind='upstream' AND status='in_progress'", (checkpoint["namespace"],)
        ).fetchone()
        last_event = cache.execute(
            "SELECT max(ended_at) FROM usage_journal WHERE namespace=?",
            (checkpoint["namespace"],)
        ).fetchone()[0]
        journal_tokens = cache.execute(
            "SELECT coalesce(sum(tokens),0) FROM usage_journal "
            "WHERE namespace=? AND kind='upstream'", (checkpoint["namespace"],)
        ).fetchone()[0]
        counter_tokens = cache.execute(
            "SELECT tokens FROM usage_totals WHERE namespace=?", (checkpoint["namespace"],)
        ).fetchone()
        mismatch = (counter_tokens[0] if counter_tokens else 0) != (
            json.loads(activation["counter_json"])["tokens"] + journal_tokens)
        state = dict(namespace=checkpoint["namespace"],
                     activated_at=activation["activated_at"],
                     cursor=max([state.get("cursor", 0)] + [r["sequence"] for r in changed]),
                     observed_at=now)
        connection.execute(
            "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (JOURNAL_KEY, json.dumps(state)),
        )
        health = dict(ok=True, mode="journal", lastSyncAt=now,
                      activatedAt=activation["activated_at"], lastMeasurementAt=last_event,
                      unknownUpstreamRequests=unknown, pendingUpstreamRequests=pending[0],
                      oldestPendingAt=pending[1], synchronizedRows=len(changed),
                      counterMismatch=mismatch)
        save_health(connection, health)
    return dict(ok=True, registered=True, mode="journal",
                synchronizedRows=len(changed), legacy=legacy, health=health)


def save_health(connection, health):
    connection.execute(
        "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (HEALTH_KEY, json.dumps(health)),
    )


def journal_request(item, checkpoint):
    return dict(id="gateway:" + item["id"], started_at=item["started_at"],
                ended_at=item["ended_at"], model=item["model"] or checkpoint["model"],
                provider_id=item["provider_id"] or checkpoint["provider_id"],
                status=item["status"], usage_known=item["tokens"] is not None,
                token_input_other=item["tokens"] or 0, token_input_cached=0, token_output=0,
                error_type=json.loads(item["info_json"]).get("error_type"))


def publish_gateway_event(connection, item, checkpoint):
    request = journal_request(item, checkpoint)
    ended = item["ended_at"] or item["started_at"]
    connection.execute("""
        INSERT INTO usage_events
            (call_id,started_at,ended_at,plugin_id,route,source,provider_id,model,
             token_input_other,token_input_cached,token_output,status,latency_ms,
             ttft_ms,error_type,attempts,usage_known,request_details,call_kind)
        VALUES (?,?,?,'unattributed','embedding/gateway','embedding_gateway_journal',
                ?,?,?,0,0,?,?,NULL,?,1,?,?,'embedding')
        ON CONFLICT(call_id) DO UPDATE SET ended_at=excluded.ended_at,
            token_input_other=excluded.token_input_other,status=excluded.status,
            latency_ms=excluded.latency_ms,error_type=excluded.error_type,
            usage_known=excluded.usage_known,request_details=excluded.request_details
    """, ("gateway:" + item["id"], item["started_at"], ended,
          request["provider_id"], request["model"], request["token_input_other"],
          item["status"], max(0, round((ended - item["started_at"]) * 1000)),
          request["error_type"], int(request["usage_known"]), json.dumps([request])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usage-db", required=True)
    parser.add_argument("--cache-db", required=True)
    parser.add_argument("--recover-plan")
    args = parser.parse_args()
    # mode=rw 避免路径错误时创建空数据库；由 astrbot 用户写入，保留 WAL 权限。
    with closing(sqlite3.connect(Path(args.usage_db).resolve().as_uri() + "?mode=rw",
                                uri=True, timeout=10)) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = (recover(connection, json.loads(Path(args.recover_plan).read_text()))
                      if args.recover_plan else sync(connection, args.cache_db))
            connection.commit()
        except Exception:
            connection.rollback()
            with connection:
                save_health(connection, dict(ok=False, lastFailureAt=time.time(),
                                             error="同步失败，请检查监控服务日志"))
            raise
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

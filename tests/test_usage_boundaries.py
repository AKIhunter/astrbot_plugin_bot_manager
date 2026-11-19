"""Execute real meter/store code with no host, credentials or network."""
import ast
import asyncio
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


def load_meter():
    tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text())
    names = {"UsageStore", "token_count", "response_usage", "request_usage"}
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
            if not module.startswith("astrbot") and getattr(node, "level", 0) == 0:
                nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SQLITE_MAX_INTEGER"
                for target in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "UsageTelemetryPlugin":
            nodes.extend(child for child in node.body if
                         isinstance(child, ast.AsyncFunctionDef) and child.name == "_persist_call")
    namespace = {"logger": logging.getLogger("offline-meter")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return SimpleNamespace(**namespace)


METER = load_meter()
INVALID = [True, -1, "bad", "", float("nan"), float("inf"), -float("inf"),
           1.5, 1 << 63, "1e1000000"]


@pytest.mark.parametrize("value", INVALID)
def test_malformed_usage_is_unknown_and_never_crashes(value):
    assert METER.token_count(value) is None
    raw = SimpleNamespace(usage=SimpleNamespace(input_other=value, input_cached=2, output=3))
    assert METER.response_usage(raw) == {
        "token_input_other": 0, "token_input_cached": 0, "token_output": 0}
    for field in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens"):
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 10}
        usage[field] = value
        assert METER.request_usage({"usage": usage}) is None


def test_valid_counts_cache_and_zero_usage():
    assert METER.token_count("42") == 42
    assert METER.token_count(0) == 0
    assert METER.request_usage({"usage": {"prompt_tokens": 0}}) == {
        "token_input_other": 0, "token_input_cached": 0, "token_output": 0}
    assert METER.request_usage({"usage": {
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 30}}}) == {
            "token_input_other": 70, "token_input_cached": 30, "token_output": 20}
    assert METER.request_usage({"usage": {"total_tokens": 42}})["token_input_other"] == 42
    assert METER.request_usage({"usage": {}}) is None


@pytest.mark.asyncio
async def test_unrepresentable_aggregate_keeps_record_and_request_readings(tmp_path):
    store = METER.UsageStore(tmp_path / "usage.sqlite3")
    plugin = SimpleNamespace(store=store, _health={"persistFailures": 0})
    state = {
        "call_id": "synthetic-call", "started_at": 1, "ended_at": 1,
        "plugin_id": "synthetic", "route": "offline", "source": "offline",
        "provider_id": "synthetic", "model": "synthetic",
        "status": "completed", "latency_ms": 0, "ttft_ms": None,
        "error_type": None, "attempts": 1, "usage_known": True,
        "_write_lock": asyncio.Lock(),
        "request_details": [
            {"token_input_other": (1 << 62), "token_input_cached": 0,
             "token_output": 0, "usage_known": True} for _ in range(2)],
    }
    await METER._persist_call(plugin, state)
    assert plugin._health["persistFailures"] == 0
    with sqlite3.connect(store.path) as database:
        row = database.execute(
            "SELECT usage_known,token_input_other,request_details FROM usage_events"
        ).fetchone()
    assert row[:2] == (0, 0)
    assert str(1 << 62) in row[2]

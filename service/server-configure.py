#!/usr/bin/env python3
"""Merge built-in provider credentials from JSON received over SSH stdin."""

import argparse
import json
import os
import sys
from pathlib import Path


FIELDS = (
    "deepseekApiKey",
    "volcAccessKeyId",
    "volcSecretAccessKey",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="/etc/bot-manager/provider-credentials.json",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("this command must run as root")
    raw = sys.stdin.read(8193)
    if len(raw) > 8192:
        parser.error("configuration payload is too large")
    try:
        source = json.loads(raw)
    except json.JSONDecodeError:
        parser.error("configuration payload is not valid JSON")
    if not isinstance(source, dict) or set(source) - set(FIELDS):
        parser.error("configuration payload contains unsupported fields")

    payload = {}
    for field in FIELDS:
        value = source.get(field, "")
        if not isinstance(value, str) or len(value) > 512:
            parser.error(f"{field} must be a string no longer than 512 characters")
        payload[field] = value.strip()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = {}
    try:
        existing = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    custom = existing.get("custom", []) if isinstance(existing, dict) else []
    next_payload = {
        "version": 2,
        "deepseek": {"apiKey": payload["deepseekApiKey"]},
        "volcengine": {
            "accessKeyId": payload["volcAccessKeyId"],
            "secretAccessKey": payload["volcSecretAccessKey"],
        },
        "custom": custom if isinstance(custom, list) else [],
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)
    os.chown(output, 0, 0)
    print("Provider balance credentials updated.")


if __name__ == "__main__":
    main()

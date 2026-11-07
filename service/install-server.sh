#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  SOURCE_DIR="$1"
else
  SOURCE_DIR="$(dirname -- "$0")"
  SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
fi
RESET_PANEL_PASSWORD="${2:-}"
case "$RESET_PANEL_PASSWORD" in
  ""|--reset-panel-password) ;;
  *)
    echo "Unknown installer option: $RESET_PANEL_PASSWORD" >&2
    exit 2
    ;;
esac
INSTALL_ROOT="/opt/bot-manager"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE_DIR="$INSTALL_ROOT/releases/$RELEASE_ID"
CURRENT_LINK="$INSTALL_ROOT/current"
UNIT_PATH="/etc/systemd/system/bot-manager.service"
CREDENTIAL_DIR="/etc/bot-manager"
CREDENTIAL_FILE="$CREDENTIAL_DIR/credentials.json"
PROVIDER_FILE="$CREDENTIAL_DIR/provider-credentials.json"
PLUGIN_TOKEN_FILE="$CREDENTIAL_DIR/plugin-token"
ASTRBOT_PLUGIN_ROOT="/var/lib/astrbot/data/plugins"
TELEMETRY_PLUGIN="astrbot_plugin_usage_telemetry"
TELEMETRY_TARGET="$ASTRBOT_PLUGIN_ROOT/$TELEMETRY_PLUGIN"
TELEMETRY_BACKUP_ROOT="/var/lib/astrbot/data/plugin-backups"
USAGE_DB="/var/lib/astrbot/data/plugin_usage.db"

# 仓库布局：插件在仓库根目录，服务在 service/。安装器需要 service/astrbot_plugin_usage_telemetry/，
# 这里从仓库根目录临时同步一份，避免手工复制导致插件与服务版本漂移。
PLUGIN_SOURCE_DIR="$SOURCE_DIR"
STAGED_PLUGIN_DIR=""
REPO_ROOT="$(dirname -- "$SOURCE_DIR")"
if [ ! -d "$SOURCE_DIR/$TELEMETRY_PLUGIN" ] && [ -f "$REPO_ROOT/metadata.yaml" ]; then
  STAGED_PLUGIN_DIR="$(mktemp -d)"
  trap 'rm -rf "$STAGED_PLUGIN_DIR"' EXIT INT TERM
  mkdir -p "$STAGED_PLUGIN_DIR/$TELEMETRY_PLUGIN"
  for file in main.py command_query.py usage_query.py search_meter.py runtime_paths.py \
              metadata.yaml _conf_schema.json; do
    if [ -f "$REPO_ROOT/$file" ]; then
      cp "$REPO_ROOT/$file" "$STAGED_PLUGIN_DIR/$TELEMETRY_PLUGIN/$file"
    fi
  done
  if [ -d "$REPO_ROOT/assets" ]; then
    cp -R "$REPO_ROOT/assets" "$STAGED_PLUGIN_DIR/$TELEMETRY_PLUGIN/assets"
  fi
  PLUGIN_SOURCE_DIR="$STAGED_PLUGIN_DIR"
  echo "Staged plugin from repository root: $REPO_ROOT"
fi
TELEMETRY_SOURCE="$PLUGIN_SOURCE_DIR/$TELEMETRY_PLUGIN"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi

for command in python3 systemctl ss; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command not found: $command" >&2
    exit 1
  fi
done

for file in server-panel.py server-agent.py server-monitor.py server-app-metrics.py server-embedding-usage.py server-configure.py panel.html bot-manager.service; do
  if [ ! -f "$SOURCE_DIR/$file" ]; then
    echo "Missing deployment file: $SOURCE_DIR/$file" >&2
    exit 1
  fi
done

for file in main.py metadata.yaml _conf_schema.json; do
  if [ ! -f "$TELEMETRY_SOURCE/$file" ]; then
    echo "Missing plugin file: $TELEMETRY_SOURCE/$file" >&2
    exit 1
  fi
done

# 遥测插件是多文件包（main + command_query/usage_query/search_meter/runtime_paths），
# 少一个就是运行时 ModuleNotFoundError，这里先卡住。
telemetry_module_count=0
for module in "$TELEMETRY_SOURCE"/*.py; do
  if [ -f "$module" ]; then
    telemetry_module_count=$((telemetry_module_count + 1))
  fi
done
if [ "$telemetry_module_count" -lt 2 ]; then
  echo "Telemetry plugin looks incomplete: $TELEMETRY_SOURCE" >&2
  exit 1
fi

previous_target="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
generated_password=""

install -d -o root -g astrbot -m 0750 "$CREDENTIAL_DIR"
if [ "$RESET_PANEL_PASSWORD" = "--reset-panel-password" ] || [ ! -f "$CREDENTIAL_FILE" ]; then
  generated_password="$(
    python3 - "$CREDENTIAL_FILE" <<'PY'
import hashlib
import json
import os
import secrets
import sys

path = sys.argv[1]
password = secrets.token_urlsafe(18)
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(
    password.encode("utf-8"),
    salt=salt,
    n=2**14,
    r=8,
    p=1,
    dklen=32,
)
payload = {
    "username": "admin",
    "salt": salt.hex(),
    "hash": digest.hex(),
}
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, separators=(",", ":"))
    handle.write("\n")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
print(password)
PY
  )"
fi
chown root:root "$CREDENTIAL_FILE"
chmod 0600 "$CREDENTIAL_FILE"
if [ ! -f "$PROVIDER_FILE" ]; then
  printf '%s\n' '{"version":2,"deepseek":{"apiKey":""},"volcengine":{"accessKeyId":"","secretAccessKey":""},"custom":[]}' > "$PROVIDER_FILE"
fi
chown root:root "$PROVIDER_FILE"
chmod 0600 "$PROVIDER_FILE"
if [ ! -f "$PLUGIN_TOKEN_FILE" ]; then
  python3 - "$PLUGIN_TOKEN_FILE" <<'PY'
import os
import secrets
import sys

path = sys.argv[1]
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    handle.write(secrets.token_urlsafe(32) + "\n")
os.chmod(temporary, 0o640)
os.replace(temporary, path)
PY
fi
chown root:astrbot "$PLUGIN_TOKEN_FILE"
chmod 0640 "$PLUGIN_TOKEN_FILE"

show_generated_credentials() {
  if [ -n "$generated_password" ]; then
    echo
    echo "Bot Manager login credentials (shown once):"
    echo "  Username: admin"
    echo "  Password: $generated_password"
    echo "Save this password in your password manager."
  fi
}

credential_notice_pending=1
show_credentials_on_failure() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$credential_notice_pending" -eq 1 ]; then
    show_generated_credentials
  fi
  exit "$status"
}
trap show_credentials_on_failure EXIT

install -d -m 0755 "$INSTALL_ROOT/releases" "$RELEASE_DIR"
install -m 0755 "$SOURCE_DIR/server-panel.py" "$RELEASE_DIR/server-panel.py"
install -m 0755 "$SOURCE_DIR/server-agent.py" "$RELEASE_DIR/server-agent.py"
install -m 0644 "$SOURCE_DIR/server-monitor.py" "$RELEASE_DIR/server-monitor.py"
# server-monitor.py 用 importlib 按文件名加载它；缺这个文件监控服务起不来。
install -m 0644 "$SOURCE_DIR/server-app-metrics.py" "$RELEASE_DIR/server-app-metrics.py"
install -m 0644 "$SOURCE_DIR/server-embedding-usage.py" "$RELEASE_DIR/server-embedding-usage.py"
install -m 0755 "$SOURCE_DIR/server-configure.py" "$RELEASE_DIR/server-configure.py"
install -m 0644 "$SOURCE_DIR/panel.html" "$RELEASE_DIR/panel.html"
install -m 0644 "$SOURCE_DIR/bot-manager.service" "$RELEASE_DIR/bot-manager.service"
install -d -m 0755 "$RELEASE_DIR/$TELEMETRY_PLUGIN"
# 整个插件目录一起搬。main.py 只是入口，command_query / usage_query /
# search_meter / runtime_paths 少任何一个都会 ModuleNotFoundError，assets 是界面资源。
cp -a "$TELEMETRY_SOURCE"/. "$RELEASE_DIR/$TELEMETRY_PLUGIN/"
rm -rf "$RELEASE_DIR/$TELEMETRY_PLUGIN/__pycache__"
chown -R root:root "$RELEASE_DIR/$TELEMETRY_PLUGIN"
find "$RELEASE_DIR/$TELEMETRY_PLUGIN" -type d -exec chmod 0755 {} +
find "$RELEASE_DIR/$TELEMETRY_PLUGIN" -type f -exec chmod 0644 {} +
install -m 0644 "$RELEASE_DIR/bot-manager.service" "$UNIT_PATH"

python3 -m py_compile \
  "$RELEASE_DIR/server-panel.py" \
  "$RELEASE_DIR/server-agent.py" \
  "$RELEASE_DIR/server-monitor.py" \
  "$RELEASE_DIR/server-app-metrics.py" \
  "$RELEASE_DIR/server-embedding-usage.py" \
  "$RELEASE_DIR/server-configure.py" \
  "$RELEASE_DIR/$TELEMETRY_PLUGIN"/*.py
(
  cd "$RELEASE_DIR"
  sha256sum \
    server-panel.py \
    server-agent.py \
    server-monitor.py \
    server-app-metrics.py \
    server-embedding-usage.py \
    server-configure.py \
    panel.html \
    bot-manager.service \
    > SHA256SUMS
  cd "$TELEMETRY_PLUGIN"
  sha256sum *.py metadata.yaml assets/* 2>/dev/null | sed "s|^\([0-9a-f]*\)  |\1  $TELEMETRY_PLUGIN/|" >> "$RELEASE_DIR/SHA256SUMS"
)
systemctl daemon-reload
systemctl enable bot-manager.service

ln -sfn "$RELEASE_DIR" "$INSTALL_ROOT/current.new"
mv -Tf "$INSTALL_ROOT/current.new" "$CURRENT_LINK"

rollback() {
  echo "Deployment verification failed; restoring the previous release." >&2
  if [ -n "$previous_target" ] && [ -d "$previous_target" ]; then
    ln -sfn "$previous_target" "$INSTALL_ROOT/current.rollback"
    mv -Tf "$INSTALL_ROOT/current.rollback" "$CURRENT_LINK"
    systemctl restart bot-manager.service || true
  else
    rm -f "$CURRENT_LINK"
    systemctl stop bot-manager.service || true
  fi
  rm -rf "$RELEASE_DIR"
  exit 1
}

if ! systemctl restart bot-manager.service; then
  rollback
fi

attempt=0
while [ "$attempt" -lt 20 ]; do
  if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:17321/api/health", timeout=1).read()' >/dev/null 2>&1; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 0.25
done

if [ "$attempt" -ge 20 ]; then
  systemctl status bot-manager.service --no-pager >&2 || true
  rollback
fi

listen_address="$(ss -ltn 2>/dev/null | awk '$4 ~ /:17321$/ {print $4; exit}')"
case "$listen_address" in
  127.0.0.1:17321) ;;
  *)
    echo "Unexpected listen address for Bot Manager: ${listen_address:-not found}" >&2
    rollback
    ;;
esac

if ! python3 - "$PLUGIN_TOKEN_FILE" <<'PY' >/dev/null 2>&1
import grp
import json
import os
import pwd
import sys
import urllib.request

account = pwd.getpwnam("astrbot")
group = grp.getgrnam("astrbot")
os.setgroups([group.gr_gid])
os.setgid(group.gr_gid)
os.setuid(account.pw_uid)
token = open(sys.argv[1], encoding="utf-8").read().strip()
request = urllib.request.Request(
    "http://127.0.0.1:17321/api/internal/deepseek-balance",
    headers={"X-Bot-Manager-Token": token},
)
with urllib.request.urlopen(request, timeout=10) as response:
    payload = json.load(response)
if payload.get("ok") is not True:
    raise SystemExit(1)
PY
then
  echo "AstrBot cannot access the internal balance endpoint." >&2
  rollback
fi

telemetry_changed=1
if [ -f "$TELEMETRY_TARGET/main.py" ] && [ -f "$TELEMETRY_TARGET/metadata.yaml" ]; then
  telemetry_changed=0
  # 逐个比对 release 里的每个插件文件，缺一个也算变更，避免运行目录被截断。
  for src in "$RELEASE_DIR/$TELEMETRY_PLUGIN"/*.py "$RELEASE_DIR/$TELEMETRY_PLUGIN/metadata.yaml"; do
    base="$(basename "$src")"
    if ! cmp -s "$src" "$TELEMETRY_TARGET/$base"; then
      telemetry_changed=1
      break
    fi
  done
  if [ "$telemetry_changed" -eq 0 ] && [ -d "$RELEASE_DIR/$TELEMETRY_PLUGIN/assets" ]; then
    if ! diff -rq "$RELEASE_DIR/$TELEMETRY_PLUGIN/assets" "$TELEMETRY_TARGET/assets" >/dev/null 2>&1; then
      telemetry_changed=1
    fi
  fi
fi

if [ "$telemetry_changed" -eq 1 ]; then
  telemetry_stage="$ASTRBOT_PLUGIN_ROOT/.${TELEMETRY_PLUGIN}.new"
  telemetry_backup="$TELEMETRY_BACKUP_ROOT/${TELEMETRY_PLUGIN}-${RELEASE_ID}"
  install -d -o astrbot -g astrbot -m 0755 "$ASTRBOT_PLUGIN_ROOT" "$TELEMETRY_BACKUP_ROOT"
  rm -rf "$telemetry_stage"
  install -d -o astrbot -g astrbot -m 0755 "$telemetry_stage"
  # 整目录替换：旧逻辑只搬 main.py + metadata.yaml，其余模块会被 mv 带走后消失。
  cp -a "$RELEASE_DIR/$TELEMETRY_PLUGIN"/. "$telemetry_stage/"
  rm -rf "$telemetry_stage/__pycache__"
  chown -R astrbot:astrbot "$telemetry_stage"
  if [ -d "$TELEMETRY_TARGET" ]; then
    mv "$TELEMETRY_TARGET" "$telemetry_backup"
  fi
  mv "$telemetry_stage" "$TELEMETRY_TARGET"

  telemetry_rollback() {
    echo "AstrBot telemetry verification failed; restoring the previous plugin." >&2
    rm -rf "$TELEMETRY_TARGET"
    if [ -d "$telemetry_backup" ]; then
      mv "$telemetry_backup" "$TELEMETRY_TARGET"
    fi
    systemctl restart astrbot.service || true
    exit 1
  }

  if ! systemctl restart astrbot.service; then
    telemetry_rollback
  fi
  attempt=0
  while [ "$attempt" -lt 40 ]; do
    if systemctl is-active --quiet astrbot.service &&
       python3 - "$USAGE_DB" <<'PY' >/dev/null 2>&1
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
connection.execute("SELECT 1 FROM usage_events LIMIT 1").fetchone()
connection.close()
PY
    then
      break
    fi
    attempt=$((attempt + 1))
    sleep 0.5
  done
  if [ "$attempt" -ge 40 ]; then
    journalctl -u astrbot.service -n 80 --no-pager >&2 || true
    telemetry_rollback
  fi
fi

echo "Bot Manager release $RELEASE_ID is active on 127.0.0.1:17321."
show_generated_credentials
credential_notice_pending=0

#!/usr/bin/env python3
"""重置 Bot Manager 服务器上三个 Web 界面的登录密码。

用法（新密码从标准输入读一行，不进 argv，避免被 ps 看到）：

    printf '%s\n' 'new-password' | python3 reset-service-password.py --target astrbot

target:
    astrbot   AstrBot WebUI（6185，账号固定 astrbot）
    snowluma  SnowLuma 控制台（5099，账号 admin）
    vnc       noVNC / x11vnc（6081，VNC 协议限制密码最长 8 位）

输出：单行 JSON，{"ok": true|false, "target": ..., "message": ..., "detail": ...}
退出码：0 成功，1 失败，2 参数错误
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ASTRBOT_CONFIG = Path("/var/lib/astrbot/data/cmd_config.json")
ASTRBOT_PORT = 6185
ASTRBOT_USER = "astrbot"
PBKDF2_ITERATIONS = 600_000

SNOWLUMA_CONTAINER = "snowluma"
SNOWLUMA_PORT = 5099
SNOWLUMA_CONFIG_DIR = Path("/app/data/config")
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_KEYLEN = 16384, 8, 1, 64

VNC_CONTAINER = SNOWLUMA_CONTAINER
VNC_PORT = 5900
VNC_PASSWORD_FILE = "vnc-password"
VNC_RFBAUTH = "/root/.vnc/passwd"
VNC_CMD = (
    "setsid x11vnc -display :1 -noxrecord -noxfixes -noxdamage "
    "-forever -threads -rfbauth " + VNC_RFBAUTH
)

MIN_LEN = 8
MAX_LEN = 128
VNC_MAX_LEN = 8


def run(cmd, timeout=60, stdin_data=None):
    result = subprocess.run(
        cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
        timeout=timeout, input=stdin_data,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(port, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(1)
    return False


def http_login(url, payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # 网络异常也算失败，但要把原因带出去
        return 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- AstrBot

def pbkdf2_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def reset_astrbot(password):
    if not ASTRBOT_CONFIG.is_file():
        return False, "找不到 AstrBot 配置", str(ASTRBOT_CONFIG)

    raw = ASTRBOT_CONFIG.read_text(encoding="utf-8-sig")
    config = json.loads(raw)
    dashboard = config.setdefault("dashboard", {})

    backup = ASTRBOT_CONFIG.with_name(
        ASTRBOT_CONFIG.name + ".bak.reset-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    backup.write_bytes(ASTRBOT_CONFIG.read_bytes())

    dashboard["password"] = hashlib.md5(password.encode("utf-8")).hexdigest()
    dashboard["pbkdf2_password"] = pbkdf2_hash(password)
    dashboard["password_storage_upgraded"] = True
    dashboard["password_change_required"] = False

    ASTRBOT_CONFIG.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8-sig"
    )

    code, output = run(["systemctl", "restart", "astrbot"], timeout=90)
    if code != 0:
        return False, "写入成功，但重启 AstrBot 失败", output.strip()[:400]

    if not wait_port(ASTRBOT_PORT, 60):
        return False, "AstrBot 重启后 60 秒内未监听 6185", ""

    status, body = http_login(
        f"http://127.0.0.1:{ASTRBOT_PORT}/api/auth/login",
        {"username": ASTRBOT_USER, "password": password},
    )
    if status == 200 and '"token"' in body:
        return True, "已重置并验证登录成功", f"备份：{backup.name}"
    return False, f"密码已写入，但登录验证失败（HTTP {status}）", body[:300]


# ---------------------------------------------------------------- SnowLuma

def container_path(container, target):
    """把容器内路径映射到宿主机路径（取最长的匹配挂载点）。"""
    code, output = run([
        "docker", "inspect", container,
        "--format", "{{range .Mounts}}{{.Destination}}|{{.Source}}\n{{end}}",
    ])
    if code != 0:
        return None
    best = None
    for line in output.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        destination, _, source = line.partition("|")
        destination = destination.rstrip("/")
        if target == destination or target.startswith(destination + "/"):
            if best is None or len(destination) > len(best[0]):
                best = (destination, source)
    if best is None:
        return None
    destination, source = best
    relative = target[len(destination):].lstrip("/")
    return Path(source) / relative if relative else Path(source)


def reset_snowluma(password):
    host_dir = container_path(SNOWLUMA_CONTAINER, str(SNOWLUMA_CONFIG_DIR))
    if host_dir is None:
        return False, "找不到 snowluma 容器的 /app/data/config 挂载", ""
    config_file = host_dir / "webui.json"

    state = {}
    if config_file.is_file():
        try:
            state = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    stat = config_file.stat() if config_file.exists() else None
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt),
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_KEYLEN,
    ).hex()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "passwordHash": digest,
        "passwordSalt": salt,
        "mustChangePassword": False,
        "generatedAt": state.get("generatedAt", now),
        "updatedAt": now,
    }
    if "totp" in state:
        payload["totp"] = state["totp"]

    temporary = config_file.with_name("webui.json.reset-tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if stat is not None:
        os.chown(temporary, stat.st_uid, stat.st_gid)
    os.chmod(temporary, 0o600)
    os.replace(temporary, config_file)

    code, output = run(["docker", "exec", SNOWLUMA_CONTAINER, "supervisorctl", "restart", "snowluma"], timeout=90)
    if code != 0:
        return False, "写入成功，但重启 snowluma 进程失败", output.strip()[:400]

    if not wait_port(SNOWLUMA_PORT, 60):
        return False, "重启后 60 秒内 5099 未监听", ""

    status, body = http_login(
        f"http://127.0.0.1:{SNOWLUMA_PORT}/api/login", {"password": password}
    )
    if status in (200, 204) and '"' in body:
        return True, "已重置并验证登录成功", "仅重启 snowluma 进程，QQ 未受影响"
    return False, f"密码已写入，但登录验证失败（HTTP {status}）", body[:300]


# ---------------------------------------------------------------- VNC

def reset_vnc(password):
    if len(password) > VNC_MAX_LEN:
        return False, f"VNC 协议只支持 8 位以内的密码（当前 {len(password)} 位）", ""

    host_dir = container_path(VNC_CONTAINER, str(SNOWLUMA_CONFIG_DIR))
    if host_dir is None:
        return False, "找不到容器的 /app/data/config 挂载", ""

    # 1) 明文写入容器配置，容器重启后仍生效
    password_file = host_dir / VNC_PASSWORD_FILE
    stat = password_file.stat() if password_file.exists() else None
    temporary = password_file.with_name(VNC_PASSWORD_FILE + ".reset-tmp")
    temporary.write_text(password + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    if stat is not None:
        os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, password_file)

    # 2) 重新生成 x11vnc 的 rfbauth 文件（密码走 stdin，不进 argv）
    code, output = run(
        ["docker", "exec", "-i", VNC_CONTAINER, "sh", "-c",
         f'read -r pw; x11vnc -storepasswd "$pw" {VNC_RFBAUTH} >/dev/null 2>&1 && echo OK'],
        stdin_data=password + "\n",
    )
    if code != 0 or "OK" not in output:
        return False, "生成 VNC 密码文件失败", output.strip()[:300]

    # 3) 重启 x11vnc：它不是 supervisord 管的进程，需要先杀再拉起
    run(["docker", "exec", VNC_CONTAINER, "sh", "-c", "pkill -f 'x11vnc -display' || true"])
    time.sleep(1)
    run(["docker", "exec", "-d", VNC_CONTAINER, "sh", "-c",
         f"{VNC_CMD} >/var/log/x11vnc.log 2>&1"])

    # 5900 只在容器内监听（未发布到宿主），所以要在容器里探活
    for _ in range(20):
        code, output = run(["docker", "exec", VNC_CONTAINER, "sh", "-c",
                            'ss -ltn 2>/dev/null | grep -q ":5900" && echo UP'])
        if "UP" in output:
            return True, "已重置 VNC 密码并重启 x11vnc", "noVNC 页面刷新后使用新密码连接"
        time.sleep(1)
    return False, "x11vnc 未能在 5900 上恢复监听，请检查容器日志", ""


TARGETS = {
    "astrbot": (reset_astrbot, MAX_LEN, "AstrBot WebUI（6185，账号 astrbot）"),
    "snowluma": (reset_snowluma, MAX_LEN, "SnowLuma 控制台（5099，账号 admin）"),
    "vnc": (reset_vnc, VNC_MAX_LEN, "noVNC / x11vnc（6081）"),
}


def main():
    parser = argparse.ArgumentParser(description="重置服务器上 Web 界面的登录密码")
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    args = parser.parse_args()

    handler, max_len, label = TARGETS[args.target]
    raw = sys.stdin.readline()
    password = raw.rstrip("\r\n")

    if len(password) < MIN_LEN:
        print(json.dumps({"ok": False, "target": args.target,
                          "message": f"密码至少 {MIN_LEN} 位"}, ensure_ascii=False))
        return 2
    if len(password) > max_len:
        print(json.dumps({"ok": False, "target": args.target,
                          "message": f"{label} 的密码最多 {max_len} 位"}, ensure_ascii=False))
        return 2
    if any(ch.isspace() for ch in password):
        print(json.dumps({"ok": False, "target": args.target,
                          "message": "密码不能包含空格等空白字符"}, ensure_ascii=False))
        return 2

    try:
        ok, message, detail = handler(password)
    except Exception as exc:  # 保底：异常也要回结构化结果
        ok, message, detail = False, f"{type(exc).__name__}: {exc}", ""

    print(json.dumps({"ok": ok, "target": args.target, "message": message,
                      "detail": detail}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

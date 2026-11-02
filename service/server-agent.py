#!/usr/bin/env python3
"""Read-only status and log collector for the local Bot Manager panel."""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


SERVICE_PORTS = {
    "astrbot": 6185,
    "snowluma": 5099,
    "novnc": 6081,
    "onebot_http": 3000,
    "onebot_ws": 3001,
    "reverse_ws": 6199,
}
SERVICE_HINTS = {
    "astrbot": ("astrbot",),
    "snowluma": ("snowluma", "snow-luma"),
    "novnc": ("novnc", "no-vnc"),
    "onebot": ("napcat", "onebot", "qq"),
}
SERVICE_CONTAINER_PORTS = {
    "astrbot": ("6185/tcp", ":6185->"),
    "snowluma": ("5099/tcp", ":5099->"),
    "novnc": ("6081/tcp", ":6081->"),
    "onebot": ("3000/tcp", "3001/tcp", ":3000->", ":3001->"),
}
SECRET_RE = re.compile(
    r"(?i)(access[_-]?token|authorization|password|passwd|secret|api[_-]?key)"
    r"(\s*[=:]\s*|\s+)([^\s,;]+)"
)


def run(command, timeout=8):
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def redact(text):
    text = SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return text


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            return True
    except OSError:
        return False


def read_first(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def os_name():
    values = {}
    for line in read_first("/etc/os-release").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    return values.get("PRETTY_NAME") or platform.platform()


def memory_info():
    values = {}
    for line in read_first("/proc/meminfo").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {"total": total, "used": max(0, total - available)}


def cpu_percent():
    def sample():
        fields = [int(item) for item in read_first("/proc/stat").splitlines()[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    try:
        total1, idle1 = sample()
        time.sleep(0.12)
        total2, idle2 = sample()
        delta = total2 - total1
        return round(100 * (1 - (idle2 - idle1) / delta), 1) if delta else 0
    except (IndexError, ValueError, ZeroDivisionError):
        return None


def docker_containers(include_all=False):
    code, output = run(["docker", "ps", "-a", "--format", "{{json .}}"])
    if code != 0:
        return []
    containers = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = item.get("Names", "")
        searchable = " ".join(
            str(item.get(key, ""))
            for key in ("Names", "Image", "Ports", "Labels")
        ).lower()
        known_port = any(
            marker in searchable
            for markers in SERVICE_CONTAINER_PORTS.values()
            for marker in markers
        )
        if include_all or known_port or any(token in searchable for token in ("astrbot", "snow", "luma", "napcat", "onebot", "novnc", "qq")):
            containers.append(
                {
                    "id": item.get("ID", ""),
                    "name": name,
                    "image": item.get("Image", ""),
                    "ports": item.get("Ports", ""),
                    "state": item.get("State", ""),
                    "status": item.get("Status", ""),
                }
            )
    return containers


def inspect_container_match(service, containers):
    names = [item.get("name") for item in containers if item.get("name")]
    if not names:
        return None, []
    code, output = run(["docker", "inspect"] + names, timeout=15)
    if code != 0:
        return None, []
    try:
        details = json.loads(output)
    except json.JSONDecodeError:
        return None, []

    hints = SERVICE_HINTS.get(service, (service,))
    port = str(SERVICE_PORTS.get(service, ""))
    best = None
    for detail in details:
        config = detail.get("Config") or {}
        host = detail.get("HostConfig") or {}
        network = detail.get("NetworkSettings") or {}
        mounts = detail.get("Mounts") or []
        searchable = json.dumps(
            {
                "name": detail.get("Name"),
                "image": config.get("Image"),
                "labels": config.get("Labels"),
                "command": config.get("Cmd"),
                "entrypoint": config.get("Entrypoint"),
                "path": detail.get("Path"),
                "args": detail.get("Args"),
                "workingDir": config.get("WorkingDir"),
                "exposedPorts": config.get("ExposedPorts"),
                "portBindings": host.get("PortBindings"),
                "ports": network.get("Ports"),
                "mountDestinations": [item.get("Destination") for item in mounts],
            },
            ensure_ascii=True,
        ).lower()
        score = sum(20 for hint in hints if hint in searchable)
        if port and (
            f'"{port}/tcp"' in searchable
            or f'":{port}"' in searchable
            or f'"{port}"' in searchable
        ):
            score += 12
        if score and (best is None or score > best[0]):
            best = (score, str(detail.get("Name") or "").lstrip("/"), mounts)
    if not best:
        return None, []
    mount_sources = [
        str(item.get("Source"))
        for item in best[2]
        if item.get("Source") and item.get("Type") == "bind"
    ]
    return best[1], mount_sources


def find_container(service, containers=None):
    containers = containers if containers is not None else docker_containers()
    hints = SERVICE_HINTS.get(service, (service,))
    for item in containers:
        searchable = (
            f"{item.get('name', '')} {item.get('image', '')} "
            f"{item.get('ports', '')}"
        ).lower()
        if any(hint in searchable for hint in hints):
            return item.get("name")
    port_markers = SERVICE_CONTAINER_PORTS.get(service, ())
    for item in containers:
        ports = str(item.get("ports", "")).lower()
        if any(marker in ports for marker in port_markers):
            return item.get("name")
    return None


def discover_container(service):
    containers = docker_containers(include_all=True)
    container = find_container(service, containers)
    if container:
        _, mounts = inspect_container_match(service, [
            item for item in containers if item.get("name") == container
        ])
        return container, mounts
    return inspect_container_match(service, containers)


def docker_stats(containers):
    names = [item["name"] for item in containers if item.get("state") == "running"]
    if not names:
        return {}
    code, output = run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"] + names,
        timeout=12,
    )
    if code != 0:
        return {}
    stats = {}
    for line in output.splitlines():
        try:
            item = json.loads(line)
            stats[item.get("Name", "")] = {
                "cpu": item.get("CPUPerc", ""),
                "memory": item.get("MemUsage", ""),
            }
        except json.JSONDecodeError:
            pass
    return stats


def discover_platforms():
    candidates = (
        "/opt/AstrBot/data/cmd_config.json",
        "/opt/astrbot/data/cmd_config.json",
        "/root/AstrBot/data/cmd_config.json",
        "/root/astrbot/data/cmd_config.json",
    )
    result = []

    def walk(value):
        if isinstance(value, dict):
            lowered = {str(key).lower(): val for key, val in value.items()}
            kind = lowered.get("type") or lowered.get("platform") or lowered.get("adapter")
            name = lowered.get("name") or lowered.get("id") or lowered.get("platform_id")
            if kind and any(word in str(kind).lower() for word in ("qq", "onebot", "aiocqhttp", "napcat")):
                entry = {
                    "name": str(name or kind)[:80],
                    "type": str(kind)[:80],
                    "enabled": bool(lowered.get("enable", lowered.get("enabled", True))),
                }
                if entry not in result:
                    result.append(entry)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for candidate in candidates:
        try:
            walk(json.loads(Path(candidate).read_text(encoding="utf-8")))
            if result:
                break
        except (OSError, json.JSONDecodeError):
            continue
    return result[:20]


def inspect_onebot_login(containers):
    container = find_container("onebot", containers)
    if not container:
        return None
    code, output = run(["docker", "inspect", container])
    if code != 0:
        return None
    try:
        details = json.loads(output)[0]
    except (json.JSONDecodeError, IndexError):
        return None
    token = ""
    env = details.get("Config", {}).get("Env") or []
    for entry in env:
        key, _, value = entry.partition("=")
        if key.lower() in ("access_token", "onebot_access_token", "token"):
            token = value
            break
    request = urllib.request.Request(
        "http://127.0.0.1:3000/get_login_info",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        data = payload.get("data") or payload
        if data.get("user_id"):
            return {
                "userId": str(data.get("user_id")),
                "nickname": str(data.get("nickname") or "QQ Bot")[:80],
            }
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


def status():
    containers = docker_containers()
    stats = docker_stats(containers)
    for item in containers:
        item.update(stats.get(item["name"], {}))

    services = []
    for name, port in SERVICE_PORTS.items():
        container_name = find_container("onebot" if name.startswith("onebot") or name == "reverse_ws" else name, containers)
        container = next((item for item in containers if item["name"] == container_name), None)
        reachable = port_open(port)
        container_status = container.get("status", "") if container else ""
        container_healthy = "unhealthy" not in container_status.lower()
        services.append(
            {
                "id": name,
                "port": port,
                "online": container_healthy
                and (
                    reachable
                    or bool(container and container.get("state") == "running")
                ),
                "reachable": reachable,
                "container": container_name,
                "status": container_status,
            }
        )

    disk = shutil.disk_usage("/")
    uptime = 0
    try:
        uptime = int(float(read_first("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        pass
    load = []
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        pass

    return {
        "ok": True,
        "timestamp": int(time.time()),
        "server": {
            "hostname": socket.gethostname(),
            "os": os_name(),
            "kernel": platform.release(),
            "uptimeSeconds": uptime,
            "cpuPercent": cpu_percent(),
            "load": load,
            "memory": memory_info(),
            "disk": {"total": disk.total, "used": disk.used},
        },
        "services": services,
        "containers": containers,
        "bots": {
            "onebot": inspect_onebot_login(containers),
            "platforms": discover_platforms(),
        },
    }


def log_attempt(attempts, source, result):
    attempts.append({"source": source[:160], "result": result})


def newest_log_file(directories):
    candidates = []
    for directory in directories:
        try:
            root = Path(directory)
            if root.is_file():
                candidates.append((root.stat().st_mtime, root))
                continue
            if not root.is_dir():
                continue
            for pattern in ("*.log", "*.txt", "logs/*.log", "log/*.log"):
                for path in root.glob(pattern):
                    if path.is_file():
                        candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def astrbot_process_ids():
    identifiers = set()
    excluded = set()
    process_id = str(os.getpid())
    while process_id.isdigit() and process_id not in excluded:
        excluded.add(process_id)
        try:
            fields = Path(f"/proc/{process_id}/stat").read_text(
                encoding="utf-8",
                errors="replace",
            ).split()
            process_id = fields[3]
        except (OSError, IndexError):
            break
    code, output = run(["ss", "-ltnp", "sport", "=", ":6185"], timeout=5)
    if code == 0:
        identifiers.update(
            process_id
            for process_id in re.findall(r"pid=(\d+)", output)
            if process_id not in excluded
        )
    code, output = run(["ps", "-eo", "pid=,args="], timeout=5)
    if code == 0:
        for line in output.splitlines():
            pid, _, command = line.strip().partition(" ")
            if (
                pid.isdigit()
                and pid not in excluded
                and "astrbot" in command.lower()
                and "server-agent.py" not in command.lower()
            ):
                identifiers.add(pid)
    for entry in Path("/proc").glob("[0-9]*/cwd"):
        try:
            if (
                entry.parent.name not in excluded
                and "astrbot" in os.readlink(entry).lower()
            ):
                identifiers.add(entry.parent.name)
        except OSError:
            continue
    return sorted(identifiers, key=int)


def order_log_lines(output, order):
    if order != "desc" or not output:
        return output
    return "\n".join(reversed(output.splitlines()))


def logs(service, lines, order="asc"):
    attempts = []
    container, mount_sources = discover_container(service)
    source = ""
    output = ""
    if container:
        code, output = run(["docker", "logs", "--tail", str(lines), "--timestamps", container], timeout=12)
        candidate_source = f"docker:{container}"
        if code == 0 and output:
            source = candidate_source
            log_attempt(attempts, candidate_source, "ok")
        else:
            log_attempt(attempts, candidate_source, "empty" if code == 0 else "unavailable")
            output = ""
        if not output and service == "astrbot":
            command = (
                "file=$(ls -1t /AstrBot/data/logs/*.log /astrbot/data/logs/*.log "
                "/app/data/logs/*.log /app/logs/*.log /root/AstrBot/data/logs/*.log "
                "/opt/AstrBot/data/logs/*.log 2>/dev/null | head -n 1); "
                "[ -n \"$file\" ] || exit 4; printf '%s\\n' \"$file\"; "
                f"tail -n {int(lines)} \"$file\""
            )
            code, candidate = run(
                ["docker", "exec", container, "sh", "-lc", command],
                timeout=12,
            )
            container_source = f"container-file:{container}"
            if code == 0 and candidate:
                path, _, output = candidate.partition("\n")
                source = f"{container_source}:{path}"
                log_attempt(attempts, source, "ok")
            else:
                log_attempt(
                    attempts,
                    container_source,
                    "empty" if code in (0, 4) else "unavailable",
                )
    else:
        log_attempt(attempts, "docker-container", "not-found")
    if not output:
        units = {
            "astrbot": ("astrbot.service", "astrbot"),
            "snowluma": ("snowluma", "snowluma.service"),
            "novnc": ("novnc", "novnc.service"),
            "onebot": ("napcat", "onebot"),
        }.get(service, ())
        for unit in units:
            code, candidate = run(
                ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"],
                timeout=10,
            )
            if code == 0 and candidate and "No entries" not in candidate:
                source = f"journal:{unit}"
                output = candidate
                log_attempt(attempts, source, "ok")
                break
            log_attempt(
                attempts,
                f"journal:{unit}",
                "empty" if code == 0 else "unavailable",
            )
    if not output and service == "astrbot":
        log_directories = [
            "/opt/AstrBot/data/logs",
            "/opt/astrbot/data/logs",
            "/root/AstrBot/data/logs",
            "/root/astrbot/data/logs",
            "/var/log/astrbot",
            "/var/log/astrbot.log",
        ]
        log_directories.extend(mount_sources)
        try:
            log_directories.extend(str(path) for path in Path("/home").glob("*/AstrBot/data/logs"))
        except OSError:
            pass
        newest = newest_log_file(log_directories)
        if newest:
            code, candidate = run(["tail", "-n", str(lines), str(newest)], timeout=10)
            if code == 0 and candidate:
                source = f"file:{newest}"
                output = candidate
                log_attempt(attempts, source, "ok")
        if not output:
            log_attempt(attempts, "host-files", "not-found")
    if not output and service == "astrbot":
        process_ids = astrbot_process_ids()
        if not process_ids:
            log_attempt(attempts, "astrbot-process", "not-found")
        for process_id in process_ids:
            process_source = f"journal:pid-{process_id}"
            code, candidate = run(
                [
                    "journalctl",
                    f"_PID={process_id}",
                    "-n",
                    str(lines),
                    "--no-pager",
                    "--output=short-iso",
                ],
                timeout=10,
            )
            if code == 0 and candidate and "No entries" not in candidate:
                source = process_source
                output = candidate
                log_attempt(attempts, source, "ok")
                break
            log_attempt(
                attempts,
                process_source,
                "empty" if code == 0 else "unavailable",
            )
            try:
                stdout_path = Path(os.readlink(f"/proc/{process_id}/fd/1"))
                if stdout_path.is_file():
                    code, candidate = run(
                        ["tail", "-n", str(lines), str(stdout_path)],
                        timeout=10,
                    )
                    if code == 0 and candidate:
                        source = f"process-file:{stdout_path}"
                        output = candidate
                        log_attempt(attempts, source, "ok")
                        break
            except OSError:
                pass
    return {
        "ok": True,
        "service": service,
        "source": source or "none",
        "order": order,
        "lines": order_log_lines(redact(output), order) if output else "",
        "attempts": attempts[-12:],
        "timestamp": int(time.time()),
    }


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    log_parser = subparsers.add_parser("logs")
    log_parser.add_argument("--service", choices=tuple(SERVICE_HINTS), required=True)
    log_parser.add_argument("--lines", type=int, default=200)
    log_parser.add_argument("--order", choices=("asc", "desc"), default="asc")
    args = parser.parse_args()

    if args.command == "status":
        payload = status()
    else:
        payload = logs(
            args.service,
            max(20, min(args.lines, 1000)),
            args.order,
        )
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

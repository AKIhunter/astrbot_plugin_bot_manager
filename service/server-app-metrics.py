#!/usr/bin/env python3
"""Bounded application resource probe; never stores process arguments."""

import hashlib
import os
import subprocess
import time
from pathlib import Path


class ApplicationProbe:
    MAX_APPLICATIONS = 24
    MAX_PROCESSES = 4096
    MAX_CGROUPS = 128
    UNITS = (
        ("astrbot", "AstrBot", "astrbot.service"),
        ("bot-manager", "Bot Manager", "bot-manager.service"),
    )

    def __init__(self, proc_root="/proc", cgroup_root="/sys/fs/cgroup",
                 clock=time.monotonic, docker_list=None, known_applications=()):
        self.proc = Path(proc_root)
        self.cgroups = Path(cgroup_root)
        self.clock = clock
        self.docker_list = docker_list or self._docker_list
        self.cores = os.cpu_count() or 1
        self.hz = os.sysconf("SC_CLK_TCK")
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.previous = {}
        self.containers = {
            item["name"].removesuffix("（容器合计）"): None
            for item in known_applications if item["id"].startswith("container-")
            and not item.get("parentId")
        }
        self.descriptions = {item["id"]: item for item in known_applications}

    @staticmethod
    def _docker_list():
        result = subprocess.run(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        if len(result.stdout) > 65536:
            raise ValueError("container discovery limit")
        containers = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t", 1)
            if len(fields) == 2 and len(fields[0]) == 64:
                containers[fields[1][:80]] = fields[0]
        return containers

    def _processes(self, group):
        pids = set()
        pending = [group]
        visited = 0
        while pending:
            directory = pending.pop()
            visited += 1
            if visited > self.MAX_CGROUPS:
                raise ValueError("cgroup discovery limit")
            pids.update(int(pid) for pid in
                        (directory / "cgroup.procs").read_text().split())
            if len(pids) > self.MAX_PROCESSES:
                raise ValueError("process discovery limit")
            pending.extend(child for child in directory.iterdir() if child.is_dir())
        processes = {}
        for pid in pids:
            try:
                raw = (self.proc / str(pid) / "stat").read_text()
                head, tail = raw.rsplit(")", 1)
                fields = tail.split()
                processes[pid] = {
                    "comm": head.partition("(")[2],
                    "parent": int(fields[1]),
                    "ticks": int(fields[11]) + int(fields[12]),
                    "start": int(fields[19]),
                    "rss": max(0, int(fields[21])) * self.page_size,
                }
            except FileNotFoundError:
                # 正常退出的进程可以忽略，权限/格式错误不能伪装成零用量。
                continue
        return processes

    def _describe(self, app_id, name, scope, memory_kind, parent=None):
        item = {"id": app_id, "name": name, "scope": scope,
                "memoryKind": memory_kind, "parentId": parent}
        self.descriptions[app_id] = item
        return item

    def _unavailable(self, descriptor):
        self.previous.pop(descriptor["id"], None)
        return {**descriptor, "state": "unavailable", "cpuPercent": None,
                "memoryBytes": None}

    def _stopped(self, descriptor):
        self.previous.pop(descriptor["id"], None)
        return {**descriptor, "state": "stopped", "cpuPercent": 0,
                "memoryBytes": 0}

    def _group_sample(self, descriptor, group, now):
        processes = self._processes(group)
        if not processes:
            return self._stopped(descriptor)
        usage = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
        ticks = int(usage["usage_usec"])
        # inode + 最早进程的启动时间用于识别服务重启，防止累计计数跳变。
        identity = (group.stat().st_ino, min(p["start"] for p in processes.values()))
        current = (now, identity, ticks)
        previous = self.previous.get(descriptor["id"])
        self.previous[descriptor["id"]] = current
        cpu = None
        if previous and previous[1] == identity and now > previous[0] and ticks >= previous[2]:
            cpu = min(100, 100 * (ticks - previous[2]) /
                      1_000_000 / (now - previous[0]) / self.cores)
        memory = int((group / "memory.current").read_text())
        stats = dict(line.split() for line in (group / "memory.stat").read_text().splitlines())
        memory = max(0, memory - int(stats.get("inactive_file", 0)))
        return {**descriptor, "state": "running" if cpu is not None else "warming",
                "cpuPercent": round(cpu, 2) if cpu is not None else None,
                "memoryBytes": memory}

    def _process_sample(self, descriptor, processes, now):
        if not processes:
            return self._stopped(descriptor)
        counters = {(pid, item["start"]): item["ticks"] for pid, item in processes.items()}
        previous = self.previous.get(descriptor["id"])
        self.previous[descriptor["id"]] = (now, counters)
        cpu = None
        if previous and now > previous[0]:
            common = counters.keys() & previous[1].keys()
            if common:
                # 新 PID 不使用进程生存期的全部 CPU，以免重载/复用时出现尖峰。
                delta = sum(max(0, counters[key] - previous[1][key]) for key in common)
                cpu = min(100, 100 * delta / self.hz / (now - previous[0]) / self.cores)
        return {**descriptor, "state": "running" if cpu is not None else "warming",
                "cpuPercent": round(cpu, 2) if cpu is not None else None,
                "memoryBytes": sum(item["rss"] for item in processes.values())}

    @staticmethod
    def _split_processes(processes):
        qq_roots = {pid for pid, item in processes.items()
                    if item["comm"].lower() in {"qq", "napcat"}}
        # Node 主进程由 supervisord 启动；其子进程跟随归属，QQ 优先。
        node_roots = {pid for pid, item in processes.items()
                      if item["comm"] in {"node", "MainThread"}
                      and processes.get(item["parent"], {}).get("comm") == "supervisord"}
        result = {"qq": {}, "web": {}, "desktop": {}}
        for pid, item in processes.items():
            ancestors = set()
            ancestor = pid
            while ancestor in processes and ancestor not in ancestors:
                ancestors.add(ancestor)
                ancestor = processes[ancestor]["parent"]
            role = "qq" if ancestors & qq_roots else "web" if ancestors & node_roots else "desktop"
            result[role][pid] = item
        return result

    def collect(self, memory_total):
        now = self.clock()
        samples = []
        for app_id, name, unit in self.UNITS:
            descriptor = self._describe(app_id, name, f"{unit} 服务及子进程",
                                        "working-set")
            group = self.cgroups / "system.slice" / unit
            try:
                if not (self.cgroups / "cgroup.controllers").exists():
                    sample = self._unavailable(descriptor)
                elif not group.exists():
                    sample = self._stopped(descriptor)
                else:
                    sample = self._group_sample(descriptor, group, now)
            except (OSError, ValueError, KeyError, IndexError):
                sample = self._unavailable(descriptor)
            samples.append(sample)

        discovery_ok = True
        try:
            active = self.docker_list()
            self.containers.update(active)
            # 容器名称限定为有界标签；不持久化容器 ID 或进程参数。
            self.containers = dict(list(self.containers.items())[-self.MAX_APPLICATIONS:])
        except (OSError, ValueError, subprocess.SubprocessError):
            active = {}
            discovery_ok = False
        retained_ids = {"container-" + hashlib.sha256(name.encode()).hexdigest()[:12]
                        for name in self.containers}
        self.descriptions = {
            key: item for key, item in self.descriptions.items()
            if not key.startswith("container-") or key in retained_ids
            or item.get("parentId") in retained_ids
        }
        self.previous = {key: value for key, value in self.previous.items()
                         if key in self.descriptions}
        for name, container_id in self.containers.items():
            if len(samples) >= self.MAX_APPLICATIONS:
                break
            app_id = "container-" + hashlib.sha256(name.encode()).hexdigest()[:12]
            descriptor = self._describe(
                app_id, f"{name}（容器合计）",
                "整个容器，包含其子应用；请勿与子应用相加", "working-set",
            )
            group = self.cgroups / "system.slice" / f"docker-{container_id}.scope"
            try:
                if not discovery_ok:
                    samples.append(self._unavailable(descriptor))
                elif name not in active:
                    samples.append(self._stopped(descriptor))
                else:
                    samples.append(self._group_sample(descriptor, group, now))
                if name in active:
                    processes = self._processes(group)
                    split = self._split_processes(processes)
                    # 仅在识别出 QQ + Web 的共享桌面容器里拆分，避免误标普通容器。
                    if split["qq"] and split["web"]:
                        for role, label in (("web", "SnowLuma"), ("qq", "QQ / NapCat"),
                                            ("desktop", "noVNC / 桌面")):
                            self._describe(
                                f"{app_id}-{role}", label,
                                "所属容器：" + name + "；" +
                                ("含 noVNC、X11、窗口管理器及容器辅助进程"
                                 if role == "desktop" else "主进程及全部子进程"),
                                "rss", app_id,
                            )
                    for child in list(self.descriptions.values()):
                        if child.get("parentId") == app_id and len(samples) < self.MAX_APPLICATIONS:
                            role = child["id"].rsplit("-", 1)[1]
                            samples.append(self._process_sample(child, split[role], now))
                else:
                    for child in list(self.descriptions.values()):
                        if child.get("parentId") == app_id and len(samples) < self.MAX_APPLICATIONS:
                            samples.append(self._stopped(child) if discovery_ok
                                           else self._unavailable(child))
            except (OSError, ValueError, KeyError, IndexError):
                # 子应用采集失败不抹掉已成功的整机/其他服务采样。
                if not any(item["id"] == app_id for item in samples):
                    samples.append(self._unavailable(descriptor))
                for child in list(self.descriptions.values()):
                    if child.get("parentId") == app_id and len(samples) < self.MAX_APPLICATIONS:
                        samples.append(self._unavailable(child))
        for sample in samples:
            memory = sample["memoryBytes"]
            sample["memoryPercent"] = (
                round(memory / memory_total * 100, 2)
                if memory is not None and memory_total > 0 else None
            )
        return samples

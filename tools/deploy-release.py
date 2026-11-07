#!/usr/bin/env python3
"""在服务器上生成新 release 目录：复制 current -> 覆盖暂存文件 -> 重算校验 -> 切链接。

用法：
    python3 deploy_release.py --id 20261108T-example-abc12345 --stage /tmp/bm-stage \
        [--restart bot-manager] [--no-flip]
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys

BASE = "/opt/bot-manager"
CURRENT = os.path.join(BASE, "current")
RELEASES = os.path.join(BASE, "releases")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.startswith("SHA256SUMS"):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def regen(root):
    lines = ["%s  %s" % (sha256(os.path.join(root, rel)), rel) for rel in tree_files(root)]
    target = os.path.join(root, "SHA256SUMS")
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, target)
    result = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=root,
                            capture_output=True, text=True)
    bad = [ln for ln in result.stdout.splitlines() if not ln.endswith(": OK")]
    return len(lines), bad


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--restart", default="")
    parser.add_argument("--no-flip", action="store_true")
    args = parser.parse_args()

    real_current = os.path.realpath(CURRENT)
    new_dir = os.path.join(RELEASES, args.id)
    if os.path.exists(new_dir):
        print("release already exists:", new_dir)
        return 1

    staged = tree_files(args.stage)
    if not staged:
        print("staging directory is empty")
        return 2

    shutil.copytree(real_current, new_dir, symlinks=True)
    for rel in staged:
        src = os.path.join(args.stage, rel)
        dst = os.path.join(new_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        print("overlay %-45s %d bytes" % (rel, os.path.getsize(dst)))

    count, bad = regen(new_dir)
    print("checksums: %d files, %d bad" % (count, len(bad)))
    for line in bad:
        print("   ", line)
    if bad:
        print("verification failed, symlink untouched")
        return 3

    if not args.no_flip:
        link = os.path.join(BASE, "current.new")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(new_dir, link)
        subprocess.run(["mv", "-T", link, CURRENT], check=True)
        print("current ->", os.path.realpath(CURRENT))

    if args.restart:
        for service in args.restart.split(","):
            subprocess.run(["systemctl", "restart", service.strip()], check=True)
            print("restarted:", service.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())

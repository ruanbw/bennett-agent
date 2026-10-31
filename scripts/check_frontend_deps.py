#!/usr/bin/env python3
"""前端双包共享依赖版本一致性守卫。

本仓库的 frontend/ 与 admin-frontend/ 是两个独立的 Vite 前端包：没有 npm/yarn
workspace,tsconfig 是逐字拷贝的两份,连共享依赖的版本一致也只靠人肉对齐——
过去没有任何机制,任何一边升级依赖都会静默漂移,直到两边构建出的组件行为
不一致才被发现。

本脚本读取两边 package.json 的 dependencies + devDependencies,取同名依赖的
交集,任一版本不一致即打印差异并以非 0 退出。CI 的 frontend 与 admin-frontend
job 各跑一次(同一脚本重复跑开销可忽略),在「构建还能过」的假象出现前拦住漂移。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 脚本在 <仓库根>/scripts/ 下,parents[1] 即仓库根;这样无论从哪个
# working-directory 调用(比如 CI 里 job 默认 working-directory 是 frontend/),
# 定位都不依赖调用方当前目录。
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("frontend", "admin-frontend")


def load_deps(package_dir: str) -> dict[str, str]:
    """读一个前端包的 dependencies + devDependencies,合并成 {名字: 版本}。"""
    path = REPO_ROOT / package_dir / "package.json"
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    merged: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        merged.update(data.get(section, {}))
    return merged


def main() -> int:
    frontend, admin = (load_deps(p) for p in PACKAGES)
    shared = sorted(set(frontend) & set(admin))

    mismatches = [
        (name, frontend[name], admin[name]) for name in shared if frontend[name] != admin[name]
    ]
    if mismatches:
        print(f"发现 {len(mismatches)} 个共享依赖版本不一致:\n")
        for name, a, b in mismatches:
            print(f"  {name}: frontend={a}  admin-frontend={b}")
        print("\n请手动对齐两边版本(本仓库无 workspace,一致性靠提交前检查)。")
        return 1

    print(f"{len(shared)} 个共享依赖版本一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())

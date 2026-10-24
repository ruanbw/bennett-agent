"""沙箱端到端冒烟：对着真实 OpenSandbox server 建机、跑命令、传文件、再销毁。

## 为什么需要这个脚本（单测已经很多了）

单测全部基于 `FakeProvider`：它们能证明**授权、生命周期、截断、审计**是对的，但证明不了
「SDK 调用在真 server 上能起来、镜像里 pandas 真能用、`tasks 超时`真被服务端杀掉、
metadata 里的归属真能读回来」。这些只有真机跑一次才知道，而且失败方式很隐蔽——
比如给命令传 `uid/gid` 会让执行结果整个丢失（`exit_code=None`、stdout 为空）。

## 怎么用

    # 1. 起一台 sandbox server（配置在 docker/opensandbox.toml）
    uvx opensandbox-server --config docker/opensandbox.toml

    # 2. 构建沙箱镜像（server 会在建机时用它）
    docker build -f docker/sandbox.Dockerfile -t bennett-sandbox:0.1 .

    # 3. 跑冒烟
    uv run python scripts/smoke_sandbox.py

不依赖数据库、不依赖登录态、不读 `SANDBOX_ENABLED`：这是 provider 的契约测试，
不是业务测试。退出码 0 = 全部通过。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# 直接跑脚本时 `app` 不在 sys.path 上（只有 `uv run python main.py` 这种仓库根的入口才在）。
# 与 scripts/seed_admin.py 同一种写法：把仓库根插到最前面，不依赖当前工作目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.sandbox.artifacts import export_workspace  # noqa: E402
from app.services.sandbox.base import SandboxError, SandboxNotFound, SandboxSpec, owner_tag  # noqa: E402
from app.services.sandbox.lease import LeaseView  # noqa: E402
from app.services.sandbox.opensandbox import OpenSandboxProvider  # noqa: E402

IMAGE = "bennett-sandbox:0.1"
WORKDIR = "/home/sandbox/workspace"


class Checker:
    """极简断言器：失败继续跑完剩下的检查，最后统一退出码。

    一次性看到所有失败比「修一个跑一次」快得多，而冒烟脚本正是那种「一次跑完看总账」的东西。
    """

    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, condition: bool, label: str, detail: str = "") -> None:
        if condition:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{f' — {detail}' if detail else ''}")
            self.failures.append(label)


async def main() -> int:
    provider = OpenSandboxProvider()
    # 同一个 user_id 派生 owner_tag 与租约视图：产物导出会带着 owner 去连接沙箱，
    # 用两个不同的 uuid 会被归属校验当成「机器不存在」（这条在第一次跑时真的踩到了）。
    user_id = uuid.uuid4()
    owner = owner_tag(user_id)
    spec = SandboxSpec(
        lease_id=f"sbx_{uuid.uuid4().hex}",
        template=IMAGE.rsplit(":", 1)[0],
        template_version=IMAGE.rsplit(":", 1)[1],
        owner_tag=owner,
        cpu=2,
        memory_mb=2048,
        workdir=WORKDIR,
    )
    check = Checker()
    ref: str | None = None
    try:
        print(f"创建沙箱（{IMAGE}）…")
        handle = await provider.create(spec)
        ref = handle.ref
        print(f"  ready in {handle.ready_ms}ms, ref={ref}")

        print("执行命令：")
        result = await provider.exec(
            ref,
            "python3 -c 'import pandas, matplotlib; print(pandas.__version__)'",
            workdir=WORKDIR,
            owner_tag=owner,
        )
        check.ok(result.exit_code == 0, "镜像里 pandas/matplotlib 可直接用", result.stderr[-300:])

        result = await provider.exec(ref, "id -u; pwd", workdir=WORKDIR, owner_tag=owner)
        check.ok("1000" in result.stdout, "以非 root（uid 1000）运行", result.stdout)

        await provider.put_file(ref, f"{WORKDIR}/notes.txt", "hello 沙箱\n".encode(), owner_tag=owner)
        back = await provider.get_file(ref, f"{WORKDIR}/notes.txt", max_bytes=1024, owner_tag=owner)
        check.ok(back.decode().startswith("hello"), "写入的文件能原样读回")

        result = await provider.exec(
            ref,
            f"cat {WORKDIR}/notes.txt && touch {WORKDIR}/notes.txt && echo writable",
            workdir=WORKDIR,
            owner_tag=owner,
        )
        check.ok("writable" in result.stdout, "运行用户对自己写的文件仍有写权限", result.stderr[-300:])

        result = await provider.exec(ref, "sleep 30", timeout_s=2, workdir=WORKDIR, owner_tag=owner)
        check.ok(result.timed_out, "超时被服务端掐断（exit -1 / timed_out）", str(result.exit_code))

        # 越权的第二道证人：metadata 里的 owner 不匹配必须当成「这台机器用不了」
        try:
            await provider.exec(ref, "echo should-not-run", workdir=WORKDIR, owner_tag=owner_tag(uuid.uuid4()))
        except SandboxNotFound:
            check.ok(True, "owner 不匹配 → SandboxNotFound（命令未执行）")
        else:
            check.ok(False, "owner 不匹配 → SandboxNotFound（命令未执行）", "命令被执行了")

        try:
            await provider.get_file(ref, f"{WORKDIR}/nope.txt", max_bytes=16, owner_tag=owner)
        except SandboxError:
            check.ok(True, "读不存在的文件 → SandboxError")
        else:
            check.ok(False, "读不存在的文件 → SandboxError")

        # 回收路径的关键一步：产物能真的打进对象存储。存储没配好时明确跳过而不是假装通过。
        lease_view = LeaseView(
            id=spec.lease_id,
            user_id=user_id,
            session_id="sess_smoke",
            provider=provider.name,
            provider_ref=ref,
            state="ready",
            artifact_uri=None,
        )
        key = await export_workspace(provider, lease_view, workdir=WORKDIR)
        if key is None:
            print("  ⏭  产物导出跳过（对象存储未配置或上传失败，见上面的 warning）")
        else:
            check.ok(key.endswith(("workspace.tar.gz", "manifest.txt")), f"产物已导出：{key}")
    finally:
        if ref is not None:
            print("销毁：")
            await provider.destroy(ref, owner_tag=owner)
            check.ok(not await provider.is_alive(ref), "destroy 之后 is_alive 为假")
            # 幂等：回收循环会重复调用
            await provider.destroy(ref, owner_tag=owner)
            check.ok(True, "重复 destroy 不报错（幂等）")

    if check.failures:
        print(f"\n失败 {len(check.failures)} 项：{', '.join(check.failures)}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SandboxError as exc:
        print(
            f"\n沙箱错误：{exc}\n"
            "检查两件事：\n"
            "  1. sandbox server 起来了吗：uvx opensandbox-server --config docker/opensandbox.toml\n"
            f"  2. 镜像构建了吗：docker build -f docker/sandbox.Dockerfile -t {IMAGE} ."
        )
        sys.exit(2)

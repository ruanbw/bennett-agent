"""销毁前把 workspace 导出到对象存储。

## 为什么这件事不能省

「按会话销毁」+「用户下次回来东西没了」是产品级事故，不是技术债。机器是可以重建的
（同一个镜像几秒就起来），用户那份算出来的结果不行。所以销毁流程固定为：

    claim(destroying) → export → provider.destroy → mark_destroyed(artifact_uri)

## 为什么先量体积再打包

`get_file` 是把内容读进内存的（provider 契约定死的），所以导出必须有硬上限：
一个 `dd if=/dev/zero of=big` 出来的 20GB 文件会把控制面进程直接 OOM。超限时退化成
**只导出清单**（文件名 + 大小），至少能告诉用户「有什么、多大」，而不是什么都没有。

## 失败策略：导出失败也要销毁

反过来（导出失败就不销毁）会让一台机器因为对象存储抖动而**永远**占着内存，而且没有
任何人会再来看它。所以这里失败只告警并返回 None，销毁继续，`lease.error` 记一笔。
代价是把「产物」降级成「尽力而为」——这个取舍写在设计文档 §4.3 里。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from app.config import settings
from app.services.sandbox.base import SandboxError, SandboxProvider
from app.services.sandbox.lease import LeaseView
from app.services.storage import ObjectStoreError, StorageNotConfigured, get_object_store

logger = logging.getLogger(__name__)

#: 打包文件在沙箱内的固定路径。固定（而不是随机）是为了让 `get_file` 失败时，
#: 运维还能连上去看一眼那个半成品。
ARCHIVE_PATH = "/tmp/bennett-workspace.tar.gz"

#: 清单最多列多少条。它只是给用户的线索，不是完整索引。
MAX_MANIFEST_ENTRIES = 200


async def export_workspace(provider: SandboxProvider, lease: LeaseView, *, workdir: str) -> str | None:
    """把 workspace 打包上传，返回对象存储 key；失败返回 None。

    `provider_ref` 为空（还在 provisioning）时直接返回 None：没有机器可导出。
    """
    if not lease.provider_ref:
        return None

    from app.services.sandbox.base import owner_tag

    owner = owner_tag(lease.user_id)
    limit = max(1, settings.sandbox_artifact_max_bytes)

    try:
        size = await _workspace_size(provider, lease.provider_ref, workdir, owner)
    except SandboxError as exc:
        logger.warning("沙箱产物：读取体积失败 (lease=%s)：%s", lease.id, exc)
        return None

    if size is None or size > limit:
        # 太大，或者量不出来：只导出清单。`size is None` 必须走这条路——
        # 把「读不到」当成「很小」正是会把控制面读爆的那一次。
        return await _export_manifest(provider, lease, workdir, owner, size=size)

    return await _export_archive(provider, lease, workdir, owner, size=size)


async def _workspace_size(
    provider: SandboxProvider, ref: str, workdir: str, owner: str
) -> int | None:
    result = await provider.exec(
        ref,
        f"du -sb --exclude={ARCHIVE_PATH} {workdir} 2>/dev/null | cut -f1",
        timeout_s=30,
        workdir=workdir,
        owner_tag=owner,
    )
    text = (result.stdout or "").strip().splitlines()
    if not text:
        return None
    try:
        return int(text[0].strip())
    except ValueError:
        return None


async def _export_manifest(
    provider: SandboxProvider,
    lease: LeaseView,
    workdir: str,
    owner: str,
    *,
    size: int | None,
) -> str | None:
    """只导出文件清单。这是「有总比没有好」的降级路径。"""
    result = await provider.exec(
        ref=lease.provider_ref or "",
        command=(
            f"find {workdir} -type f -printf '%s\\t%p\\n' 2>/dev/null | sort -rn "
            f"| head -n {MAX_MANIFEST_ENTRIES}"
        ),
        timeout_s=60,
        workdir=workdir,
        owner_tag=owner,
    )
    header = "工作目录超过导出上限，以下只是文件清单（按大小降序）。\n"
    if size is not None:
        header += f"总量约 {size} 字节。\n"
    body = header + (result.stdout or "")

    key = _artifact_key(lease, "manifest.txt")
    return await _upload(key, body.encode(), "text/plain; charset=utf-8")


async def _export_archive(
    provider: SandboxProvider,
    lease: LeaseView,
    workdir: str,
    owner: str,
    *,
    size: int,
) -> str | None:
    ref = lease.provider_ref or ""
    packed = await provider.exec(
        ref,
        # `-C workdir .`：归档里是相对路径，解包时不会把宿主的绝对路径带进去。
        f"tar czf {ARCHIVE_PATH} -C {workdir} . 2>/dev/null && stat -c%s {ARCHIVE_PATH}",
        timeout_s=120,
        workdir=workdir,
        owner_tag=owner,
    )
    if packed.exit_code not in (0, None) or packed.timed_out:
        logger.warning("沙箱产物：打包失败 (lease=%s) exit=%s", lease.id, packed.exit_code)
        return None

    # 压缩后可能比原始体积小很多，但不会更小到离谱；给 1.5 倍余量后仍然用配置上限兜住。
    budget = min(settings.sandbox_artifact_max_bytes, int(size * 1.5) + 1024 * 1024)
    try:
        blob = await provider.get_file(ref, ARCHIVE_PATH, max_bytes=budget, owner_tag=owner)
    except SandboxError as exc:
        logger.warning("沙箱产物：取回归档失败 (lease=%s)：%s", lease.id, exc)
        return None

    return await _upload(_artifact_key(lease, "workspace.tar.gz"), blob, "application/gzip")


async def _upload(key: str, data: bytes, content_type: str) -> str | None:
    """上传字节。对象存储未配置/抖动时只告警——销毁不能被它挡住。"""
    try:
        store = get_object_store()
    except StorageNotConfigured:
        logger.warning("沙箱产物：对象存储未配置，跳过导出")
        return None

    path = None
    try:
        # 接口只收本地路径（`put_file`），所以落一次临时文件；用后即删。
        fd, path = tempfile.mkstemp(prefix="sbx-artifact-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        await store.put_file(key, path, content_type)
        return key
    except (ObjectStoreError, OSError) as exc:
        logger.warning("沙箱产物：上传失败 (key=%s)：%s", key, exc)
        return None
    finally:
        if path:
            await asyncio.to_thread(lambda: os.path.exists(path) and os.unlink(path))


def _artifact_key(lease: LeaseView, filename: str) -> str:
    """对象键：`sandbox/{user_id}/{lease_id}/{filename}`。

    带 user_id 是为了让运维按人清理，带 lease_id 是因为同一会话会重建多次环境
    （`sandbox_reset`），每次的产物必须分开——否则后一次的导出会覆盖前一次。
    """
    return f"sandbox/{lease.user_id}/{lease.id}/{filename}"

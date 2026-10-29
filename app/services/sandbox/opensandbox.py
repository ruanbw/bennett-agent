"""OpenSandbox 后端：本地 Docker 与生产 K8s/Firecracker 共用同一 API 与实现。

两个实测坑：命令不能传 `uid/gid`（会让 exit_code 变 None、stdout 为空）；超时的表现是
exit_code==-1 + CommandExecError 而非抛异常，必须翻成 `timed_out=True`。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxException as OpenSandboxError
from opensandbox.models.execd import RunCommandOpts

from app.config import settings
from app.services.sandbox.base import (
    MAX_EXEC_TIMEOUT_S,
    ExecResult,
    SandboxCapacityError,
    SandboxError,
    SandboxHandle,
    SandboxNotFound,
    SandboxSpec,
)

#: 退出码：SDK 用它表示「命令被服务端杀掉」（超时）。
KILLED_EXIT_CODE = -1

#: 沙箱内 execd 的工作目录由服务端与镜像共同决定，这里只兜底。
DEFAULT_WORKDIR = "/home/sandbox/workspace"


def _memory_spec(memory_mb: int) -> str:
    """把 MB 转成 OpenSandbox 的资源写法（`Gi` / `Mi`）。

    整除 1024 用 `Gi`：配置里本来写的是 4096 这种整数，渲染成 `4Gi` 更接近运维读法。
    """
    if memory_mb % 1024 == 0:
        return f"{memory_mb // 1024}Gi"
    return f"{memory_mb}Mi"


class OpenSandboxProvider:
    """`SandboxProvider` 的 OpenSandbox 实现。"""

    name = "opensandbox"

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base_url = (base_url or settings.sandbox_server_url or "").strip()
        self._api_key = api_key or settings.sandbox_server_api_key or None

    def _config(self) -> ConnectionConfig:
        """每次新建：`ConnectionConfig` 持有 httpx transport，跨事件循环复用会坏。

        SDK 要求 async transport 只在单个 event loop 内用；生产只有一个循环，但测试会换循环。
        """
        if not self._base_url:
            raise SandboxError("未配置 SANDBOX_SERVER_URL：沙箱服务地址为空。")
        return ConnectionConfig(domain=self._base_url, protocol="http", api_key=self._api_key)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _wrap(exc: OpenSandboxError, action: str) -> SandboxError:
        """把 SDK 异常翻成契约里的三类之一。

        只保留类名与状态码，不带 traceback/响应体：它们可能含沙箱内部路径，最终会进入模型上下文。
        """
        status = getattr(exc, "status_code", None)
        message = str(exc)
        if status == 404 or "not found" in message.lower():
            return SandboxNotFound(f"沙箱{action}失败：机器不存在或已回收。")
        return SandboxError(f"沙箱{action}失败（{type(exc).__name__}，status={status}）。")

    async def _connect(self, ref: str, owner_tag: str | None) -> Sandbox:
        """连接并核对归属；拿不到机器与归属不符都返回 `SandboxNotFound`。

        两者故意不区分：对外文案必须一致，否则就成了「这个 id 存在吗」的探测器。
        """
        try:
            sandbox = await Sandbox.connect(ref, connection_config=self._config(), skip_health_check=True)
        except OpenSandboxError as exc:
            raise self._wrap(exc, "连接") from exc

        if owner_tag is not None:
            try:
                info = await sandbox.get_info()
            except OpenSandboxError as exc:
                await self._close_quietly(sandbox)
                raise self._wrap(exc, "读取元数据") from exc
            actual = (getattr(info, "metadata", None) or {}).get("owner")
            if actual != owner_tag:
                await self._close_quietly(sandbox)
                raise SandboxNotFound("沙箱连接失败：机器不存在或已回收。")
        return sandbox

    @staticmethod
    async def _close_quietly(sandbox: Sandbox) -> None:
        # 关闭失败不影响结果：连接本来就该丢，SDK 会自己回收 transport。
        with contextlib.suppress(Exception):
            await sandbox.close()

    # ------------------------------------------------------------------
    # 契约
    # ------------------------------------------------------------------
    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        started = time.monotonic()
        image = f"{spec.template}:{spec.template_version}"
        try:
            sandbox = await Sandbox.create(
                image,
                connection_config=self._config(),
                # 归属写在服务端持久化的 metadata 里，供之后每次操作核对。
                metadata={"lease": spec.lease_id, "owner": spec.owner_tag},
                resource={"cpu": f"{spec.cpu:g}", "memory": _memory_spec(spec.memory_mb)},
                # 服务端侧的硬上限：控制面崩了也不会留下永久机器（与租约的 hard_ttl 同源）。
                timeout=_seconds(settings.sandbox_hard_ttl_seconds),
            )
        except OpenSandboxError as exc:
            message = str(exc).lower()
            if "rate" in message or "limit" in message or "quota" in message or "capacity" in message:
                raise SandboxCapacityError("沙箱服务资源已满，请稍后再试。") from exc
            raise self._wrap(exc, "创建") from exc

        ready_ms = int((time.monotonic() - started) * 1000)
        await self._close_quietly(sandbox)
        return SandboxHandle(ref=sandbox.id, ready_ms=ready_ms)

    async def exec(
        self,
        ref: str,
        command: str,
        *,
        timeout_s: int = 120,
        workdir: str | None = None,
        owner_tag: str | None = None,
    ) -> ExecResult:
        effective = max(1, min(int(timeout_s), MAX_EXEC_TIMEOUT_S))
        sandbox = await self._connect(ref, owner_tag)
        started = time.monotonic()
        try:
            execution = await sandbox.commands.run(
                command,
                opts=RunCommandOpts(
                    working_directory=workdir or DEFAULT_WORKDIR,
                    timeout=_seconds(effective),
                ),
            )
        except OpenSandboxError as exc:
            raise self._wrap(exc, "执行") from exc
        finally:
            await self._close_quietly(sandbox)

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = _join_logs(execution.logs.stdout)
        stderr = _join_logs(execution.logs.stderr)
        exit_code = execution.exit_code
        timed_out = exit_code == KILLED_EXIT_CODE
        if timed_out and not stderr:
            # 被杀掉的命令通常没有 stderr；补一句，避免模型以为「什么都没有发生」。
            stderr = "命令被服务端终止（超时）。"
        if exit_code is None and execution.error is not None:
            # 既没有退出码也没有被标记为 kill：这是执行侧的失败，不是「命令返回非零」。
            raise SandboxError(f"沙箱执行失败：{execution.error.name}")
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    async def put_file(self, ref: str, path: str, data: bytes, *, owner_tag: str | None = None) -> None:
        sandbox = await self._connect(ref, owner_tag)
        try:
            await sandbox.files.write_file(path, data)
        except OpenSandboxError as exc:
            raise self._wrap(exc, "写入文件") from exc
        finally:
            await self._close_quietly(sandbox)

    async def get_file(self, ref: str, path: str, *, max_bytes: int, owner_tag: str | None = None) -> bytes:
        sandbox = await self._connect(ref, owner_tag)
        try:
            # 必须走二进制接口：契约返回 bytes，而产物导出要读的是 `.tar.gz`。用
            # `read_file(encoding="utf-8")` 会抛 UnicodeDecodeError（非 SDK 异常，会冒到调用方）。
            raw = await sandbox.files.read_bytes(path)
        except OpenSandboxError as exc:
            raise self._wrap(exc, "读取文件") from exc
        finally:
            await self._close_quietly(sandbox)

        if len(raw) > max_bytes:
            # 不静默截断：截断的 JSON / 二进制会让模型在一个坏文件上继续推理。
            raise SandboxError(f"文件过大：{len(raw)} 字节，超过上限 {max_bytes} 字节。")
        return raw

    async def destroy(self, ref: str, *, owner_tag: str | None = None) -> None:
        """幂等：机器已经不在时正常返回（回收循环会重复调用）。"""
        try:
            sandbox = await self._connect(ref, owner_tag)
        except SandboxNotFound:
            return
        try:
            await sandbox.destroy()
        except OpenSandboxError as exc:
            wrapped = self._wrap(exc, "销毁")
            if isinstance(wrapped, SandboxNotFound):
                return
            raise wrapped from exc
        finally:
            await self._close_quietly(sandbox)

    async def is_alive(self, ref: str) -> bool:
        """「还能用吗」而不是「记录还在吗」：连接（含 endpoint 解析）失败即视为不可用。"""
        try:
            sandbox = await self._connect(ref, owner_tag=None)
        except SandboxError:
            return False
        await self._close_quietly(sandbox)
        return True


def _seconds(value: int | float) -> Any:  # noqa: ANN401 - 返回 timedelta，参数是秒
    from datetime import timedelta

    return timedelta(seconds=float(value))


def _join_logs(messages: Any) -> str:  # noqa: ANN401 - SDK 的日志模型
    """把 SDK 的日志分片拼回纯文本。

    分片按行给出，有的自带换行有的不带，所以按需补换行而不是直接 `"".join`。
    """
    if not messages:
        return ""
    parts: list[str] = []
    for message in messages:
        text = getattr(message, "text", "") or ""
        parts.append(text)
        if text and not text.endswith("\n"):
            parts.append("\n")
    return "".join(parts)

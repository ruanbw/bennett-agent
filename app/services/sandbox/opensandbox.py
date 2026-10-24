"""OpenSandbox 后端：生产与本地共用同一套 API。

## 为什么是这个后端（取代先前的 docker / e2b 两个实现）

OpenSandbox（Apache-2.0，Linux Foundation，Alibaba 贡献）同时提供两种运行时：
本地 **Docker**、生产 **Kubernetes + Firecracker**（"Fast Sandbox"）。于是
「本地开发」与「线上多租户」走**同一个 API、同一个 provider 实现**——
少一整类「本地跑得通、上线行为不同」的故障。先前的 `docker.py`（自己拼 `docker run`）
因此被删除：它只能在本地用，却要单独维护一套命令拼装与超时处理。

附带拿到两件本来要自己写的东西：per-sandbox 出站策略（egress sidecar）与
Credential Vault。本文件只实现契约要求的六个方法，策略在服务端配置
（`docker/opensandbox.toml`）。

## 模型：`ref` 是 OpenSandbox 的 sandbox id

契约（`app/services/sandbox/base.py`）里 `ref` 只在服务端流动，模型看不到也传不进。
每次操作都要先 `Sandbox.connect(ref)`——这一步本身就是「这台机器还在吗」的探针，
所以 `is_alive` 与「不存在」的判定共用同一段代码。

## 归属校验（第二道证人）

`create` 时把 `lease` 与 `owner` 写进 sandbox 的 **metadata**（服务端持久化），
之后每次 `exec/destroy/...` 都从 `get_info().metadata` 读回来比对。
授权已经在作用域层完成（模型拿不到 ref），这一道防的是「库里的行和实际机器对不上」
——迁移回滚、并发写错行这类事故里，只有机器自己的记录能当证人。

## 两个实测出来的坑（改这里之前先读）

1. **不要给命令传 `uid/gid`**。`RunCommandOpts(uid=1000, gid=1000)` 在本版本会让
   执行结果丢失（`exit_code=None`、stdout 为空）——实测复现。镜像本身已经是
   `USER 1000`、`WORKDIR /home/sandbox/workspace`，不需要在命令层再指定。
2. **超时的表现是 `exit_code == -1` + `error.name == "CommandExecError"`**
   （traceback 里是 `signal: killed`），不是抛异常。所以超时必须翻译成
   `ExecResult(timed_out=True)` 交给上层，而不是当成环境故障。
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

    整除 1024 时用 `Gi`：配置里本来就写 4096 这种整数，渲染成 `4Gi` 比 `4096Mi`
    更接近运维的读法，也避免某些调度器对超大 `Mi` 数值的处理差异。
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

        SDK 文档明确要求「Async transports are generally expected to be used within a
        single asyncio event loop」。生产只有一个循环，但测试会换循环，所以不缓存。
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

        只保留异常类名与状态码，**不带 traceback/响应体**：它们可能包含沙箱内部路径，
        最终会经由工具层进入模型上下文。
        """
        status = getattr(exc, "status_code", None)
        message = str(exc)
        if status == 404 or "not found" in message.lower():
            return SandboxNotFound(f"沙箱{action}失败：机器不存在或已回收。")
        return SandboxError(f"沙箱{action}失败（{type(exc).__name__}，status={status}）。")

    async def _connect(self, ref: str, owner_tag: str | None) -> Sandbox:
        """连接并核对归属。拿不到机器 → `SandboxNotFound`；归属不符同样 → `SandboxNotFound`。

        归属不符故意**不**区分于不存在：对外文案必须一致，否则就成了「这个 id 存在吗」
        的探测器（见设计文档 §7）。
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
            # **必须走二进制接口**：契约返回 bytes，而产物导出要读的是 `.tar.gz`。
            # 用 `read_file(encoding="utf-8")` 会在第一个非 UTF-8 字节上抛
            # `UnicodeDecodeError`（实测：tar.gz 的第 2 个字节就是 0x8b），
            # 而那既不是 SDK 异常也不是我们的 `SandboxError`，会一路冒到调用方。
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

    分片按行给出；直接 `"".join` 会丢掉行尾（有些分片自带、有些不带），所以按需补换行。
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

"""沙箱 provider 契约（冻结接口）：换后端不该动业务代码。
`SandboxHandle.ref` 只在服务端流动、从不出现在工具签名里；失败必须用异常表达，否则工具层只能猜。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: 输出截断阈值：一次 `npm install` 就能把一轮对话的 token 成本推高一个数量级。
#: 截断在渲染层做，这里只是阈值来源。
MAX_OUTPUT_HEAD_CHARS = 4_000
MAX_OUTPUT_TAIL_CHARS = 2_000

#: 单条命令默认超时。provider 必须自己保证到点终止：取消协程后容器里的进程还在跑，
#: 下一次 exec 会和它抢同一个 workspace。
DEFAULT_EXEC_TIMEOUT_S = 120
MAX_EXEC_TIMEOUT_S = 900


class SandboxError(RuntimeError):
    """沙箱操作的基类异常。工具层统一把它翻译成给模型看的中文文案。"""


class SandboxNotFound(SandboxError):
    """句柄不存在或已经回收；不要用它区分「不存在」与「不属于你」。
    对外文案必须一致，否则就成了存在性探测器。
    """


class SandboxCapacityError(SandboxError):
    """节点池/并发已满。工具层要回「当前资源已满，请稍后再试」，而不是假装成功。"""


class SandboxTimeout(SandboxError):
    """命令超时。与 `SandboxError` 分开是因为文案不同：要让它缩小范围/加 timeout，
    而不是以为环境坏了去重建沙箱。"""


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """创建一台沙箱所需的一切。
    `owner_tag` 必须写进 provider 元数据并比对：它防的是「库里的行和实际机器对不上」。
    """

    lease_id: str
    template: str
    template_version: str
    owner_tag: str
    cpu: float
    memory_mb: int
    #: 工作目录（沙箱内绝对路径）。provider 保证它存在且可写。
    workdir: str = "/home/sandbox/workspace"


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    ref: str
    #: 从调用到可执行第一条命令的耗时（毫秒），用于观测冷启动，不参与业务判断。
    ready_ms: int


@dataclass(frozen=True, slots=True)
class ExecResult:
    """一条命令的执行结果。
    `stdout` / `stderr` 是未截断的原文，截断由 `app/tools/sandbox.py:render_exec_result` 负责。
    """

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def output_bytes(self) -> int:
        return len(self.stdout.encode()) + len(self.stderr.encode())


@runtime_checkable
class SandboxProvider(Protocol):
    """沙箱后端。实现必须幂等、并发安全，并自己保证资源不泄漏。"""

    #: 落库用（`sandbox_lease.provider`）。换后端时旧租约要靠它判断该找谁回收。
    name: str

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        """创建并等到可执行。失败抛 `SandboxCapacityError` / `SandboxError`。"""

    async def exec(
        self,
        ref: str,
        command: str,
        *,
        timeout_s: int = DEFAULT_EXEC_TIMEOUT_S,
        workdir: str | None = None,
        owner_tag: str | None = None,
    ) -> ExecResult:
        """在沙箱内跑一条 shell 命令。超时由 provider 在沙箱内部保证，返回 `timed_out=True` 而非抛错。
        `owner_tag` 非空时必须核对机器自己的标记，不一致抛 `SandboxNotFound`。
        """

    async def put_file(self, ref: str, path: str, data: bytes, *, owner_tag: str | None = None) -> None:
        """写入文件（父目录不存在时创建）。`owner_tag` 语义同 `exec`。"""

    async def get_file(self, ref: str, path: str, *, max_bytes: int, owner_tag: str | None = None) -> bytes:
        """读取文件。超过 `max_bytes` 抛 `SandboxError`，不要静默截断——坏文件只会在下游炸。
        `owner_tag` 语义同 `exec`。"""

    async def destroy(self, ref: str, *, owner_tag: str | None = None) -> None:
        """销毁。幂等：已经不存在时正常返回，回收循环会重复调用。
        `owner_tag` 语义同 `exec`：不匹配时抛 `SandboxNotFound`，不要删别人的机器。
        """

    async def is_alive(self, ref: str) -> bool:
        """ref 是否仍然可用。用于回收前确认与「库里有、机器没了」的核对。"""


def owner_tag(user_id: uuid.UUID | str) -> str:
    """归属标记：`sha256(user_id)[:16]`。
    用哈希而非原始 uuid：容器 label / provider metadata 会出现在日志与 `docker inspect` 里。
    """
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:16]


__all__ = [
    "DEFAULT_EXEC_TIMEOUT_S",
    "MAX_EXEC_TIMEOUT_S",
    "MAX_OUTPUT_HEAD_CHARS",
    "MAX_OUTPUT_TAIL_CHARS",
    "ExecResult",
    "SandboxCapacityError",
    "SandboxError",
    "SandboxHandle",
    "SandboxNotFound",
    "SandboxProvider",
    "SandboxSpec",
    "SandboxTimeout",
    "owner_tag",
]

"""沙箱 provider 契约（冻结接口）。

## 这个文件为什么存在，以及它唯一的目标

后端要能换：本期跑 E2B Runtime（Firecracker microVM），本地与 CI 跑 Docker，
将来可能换 Cloud Hypervisor。**换后端不应该动业务代码**，所以这里只定义
「上层需要 provider 做什么」，不定义任何实现细节。

契约刻意做小（6 个方法）。每多一个方法，就多一个后端必须实现、必须测、必然分叉的点。
下面这些**故意不在契约里**：

- 不暴露端口映射、不管理浏览器、不做快照/分叉 —— 那是 P2 及以后的事；
- 不接受 provider 自己的原生参数（typed dict 只放我们要的），否则后端一换就漏；
- 不提供「列出我全部沙箱」这类查询：租约表是唯一真相，provider 不是数据库。

## ref 是什么，以及它为什么不能出现在工具参数里

`SandboxHandle.ref` 是 provider 侧的句柄：docker 下是容器名，E2B 下是它自己的 sandbox id。
它**只在服务端流动**，从不出现在模型可见的工具签名里——见
`docs/design/sandbox-vm.md` §6：模型手里没有可伪造的句柄，越权在构造上不可达。

## 错误为什么是异常而不是返回值

工具层需要把失败翻译成中文并决定是否重试；如果 provider 把「不存在」「超时」
「池满」都摊平成 `ExecResult(exit_code=-1)`，工具层就只能猜。异常类型是三个后端
唯一必须达成一致的语义集，所以放在契约里。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: 输出截断阈值。沙箱是本项目唯一的「成本控制点」在 exec 输出上：
#: 一次 `npm install` 就能把一轮对话的 token 成本推高一个数量级，
#: 而账单上看起来只是「用户话很多」。见设计文档 §8。
MAX_OUTPUT_HEAD_CHARS = 4_000
MAX_OUTPUT_TAIL_CHARS = 2_000

#: 单条命令默认超时。provider 必须自己保证到点终止，而不是让上层取消协程——
#: 协程取消之后容器里的进程还在跑，下一次 exec 会和它抢同一个 workspace。
DEFAULT_EXEC_TIMEOUT_S = 120
MAX_EXEC_TIMEOUT_S = 900


class SandboxError(RuntimeError):
    """沙箱操作的基类异常。工具层统一把它翻译成给模型看的中文文案。"""


class SandboxNotFound(SandboxError):
    """句柄不存在或已经回收。**不要**用它区分「不存在」与「不属于你」——

    授权由作用域在构造上保证（模型拿不到句柄），这里只表达「这个 ref 用不了」。
    对外文案必须一致，否则就成了存在性探测器。
    """


class SandboxCapacityError(SandboxError):
    """节点池/并发已满。工具层要回「当前资源已满，请稍后再试」，而不是假装成功。"""


class SandboxTimeout(SandboxError):
    """命令超时。与 `SandboxError` 分开，是因为文案完全不同：

    超时要告诉模型「缩小范围/加 timeout」，而不是「环境坏了」——后者会让它去重建沙箱。
    """


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """创建一台沙箱所需的一切。

    `owner_tag` 是归属标记（`owner_tag(user_id)`），provider 必须把它写进**它自己的**
    元数据里（docker 是容器 label，E2B 是 metadata），并在后续操作前比对一次。
    这不替代授权——授权在作用域层已经完成——它防的是「库里的行和实际机器对不上」
    （迁移回滚、并发写错行），那种时候只有 provider 侧自己的记录能当第二个证人。
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

    `stdout` / `stderr` 是**原始**（未截断）文本，截断由渲染层做
    （`app/tools/sandbox.py` 的 `render_exec_result`）：provider 只负责如实返回，
    截断规则属于「怎么给模型看」，改规则不该动 provider。
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
    """沙箱后端。实现必须**幂等**、**并发安全**，并自己保证资源不泄漏。"""

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
        """在沙箱内跑一条 shell 命令。

        超时由 provider 在沙箱内部保证（docker 下是 `timeout(1)`），返回
        `ExecResult(timed_out=True)` 而不是抛异常——超时是正常结果，模型需要看到它。

        `owner_tag` 非空时，provider **必须**核对自己记录的归属标记（docker 容器 label、
        E2B sandbox metadata），不一致就抛 `SandboxNotFound`。这是第二道证人：
        授权已经在作用域层完成，这一道防的是「库里的行与实际机器对不上」
        （迁移回滚、并发写错行）——那种时候唯一可信的是机器自己身上的标记。
        """

    async def put_file(self, ref: str, path: str, data: bytes, *, owner_tag: str | None = None) -> None:
        """写入文件（父目录不存在时创建）。`owner_tag` 语义同 `exec`。"""

    async def get_file(
        self, ref: str, path: str, *, max_bytes: int, owner_tag: str | None = None
    ) -> bytes:
        """读取文件。超过 `max_bytes` 由实现抛 `SandboxError`，不要静默截断——
        截断的二进制/JSON 会让模型在一个坏文件上继续推理。`owner_tag` 语义同 `exec`。"""

    async def destroy(self, ref: str, *, owner_tag: str | None = None) -> None:
        """销毁。**幂等**：已经不存在时正常返回。回收循环会重复调用它。

        `owner_tag` 语义同 `exec`：不匹配时抛 `SandboxNotFound` 而不是去删别人的机器。
        """

    async def is_alive(self, ref: str) -> bool:
        """ref 是否仍然可用。用于回收前确认与「库里有、机器没了」的核对。"""


def owner_tag(user_id: uuid.UUID | str) -> str:
    """归属标记：`sha256(user_id)[:16]`。

    用哈希而不是原始 uuid：容器 label / provider metadata 可能出现在日志、
    `docker inspect` 输出、运维截图里，能不出现就不出现用户主键。
    截断到 16 hex（64 bit）足够做「同一性比对」，碰撞风险与本用途无关。
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

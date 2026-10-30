"""沙箱工具：让模型在一台可丢弃的隔离环境里跑命令、读写文件。
工具签名里没有 sandbox_id / user_id：租约由服务端按 `current_sandbox_scope()` 解析，模型手里没有
可伪造的句柄，越权在构造上不可达；所有失败回同一句文案（不给存在性 oracle），且只返回文本不抛异常。
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from app.config import settings
from app.runtime.scopes import current_sandbox_scope
from app.services.sandbox import get_provider, lease_service
from app.services.sandbox.base import (
    DEFAULT_EXEC_TIMEOUT_S,
    MAX_EXEC_TIMEOUT_S,
    MAX_OUTPUT_HEAD_CHARS,
    MAX_OUTPUT_TAIL_CHARS,
    ExecResult,
    SandboxCapacityError,
    SandboxError,
    SandboxNotFound,
    SandboxTimeout,
    owner_tag,
)
from app.services.sandbox.lease import STATE_PROVISIONING, LeaseView

logger = logging.getLogger(__name__)

#: 没有作用域时的统一口径；假装有默认身份会让所有匿名调用挤到同一台机器上。
NO_SCOPE_MESSAGE = (
    "当前会话没有关联用户（未登录），无法使用执行环境。请直接依据对话内容回答，并说明你没有可用的执行环境。"
)

#: 分开写是因为模型的下一步不同：池满该等、超时该缩小范围、不存在该重试（重试会重建）。
ERR_CAPACITY = "当前执行环境资源已满，请稍后再试。"
ERR_NOT_FOUND = "执行环境不存在或已回收，请重试；下次执行会自动创建新环境。"
ERR_TIMEOUT = "命令超时（可调大 timeout_s 或缩小范围）。"
ERR_GENERIC = "执行环境暂时不可用，请稍后再试。"

#: 单次写入字符上限；超限必须拒绝而非静默截断，半截脚本要到下一次执行才炸且模型不自知。
MAX_WRITE_CHARS = 200_000

#: 单次读取字节上限，也是 `sandbox_read_file` 的 `max_bytes` 天花板：整份大文件进上下文就是一次 token 事故。
MAX_READ_BYTES = 200_000

#: stderr 阈值比 stdout 短：几万行 stderr 多是重复报错，对新信息没有贡献。
STDERR_HEAD_CHARS = 1_500
STDERR_TAIL_CHARS = 1_500


# ---------------------------------------------------------------------
# 渲染（纯函数，可单测）
# ---------------------------------------------------------------------
def _truncate(text: str, *, head_chars: int, tail_chars: int) -> tuple[str, int]:
    """保留头尾、中间标注省略字符数，返回 `(展示文本, 省略数)`。
    必须留尾：关键信息常在最后（安装结论、报错末行、`tail` 的输出），只截头会让模型看 4000 个进度条。
    """
    if len(text) <= head_chars + tail_chars:
        return text, 0
    omitted = len(text) - head_chars - tail_chars
    return f"{text[:head_chars]}\n……（中间省略 {omitted:,} 个字符）……\n{text[-tail_chars:]}", omitted


def _format_duration(duration_ms: int) -> str:
    if duration_ms < 1_000:
        return f"{duration_ms}ms"
    return f"{duration_ms / 1_000:.1f}s"


def render_exec_result(result: ExecResult, command: str) -> str:
    """把一次执行结果渲染成给模型看的文本（纯函数，截断规则可单测）。
    沙箱花费只通过「exec 输出进上下文」变成 token；超时要写成「命令超时」，写「环境坏了」会让
    模型去重建沙箱重跑同一条命令，只多花一次冷启动。
    """
    stdout_text, stdout_omitted = _truncate(
        result.stdout, head_chars=MAX_OUTPUT_HEAD_CHARS, tail_chars=MAX_OUTPUT_TAIL_CHARS
    )
    stderr_text, stderr_omitted = _truncate(result.stderr, head_chars=STDERR_HEAD_CHARS, tail_chars=STDERR_TAIL_CHARS)

    parts: list[str] = []
    if result.timed_out:
        parts.append("命令超时（可调大 timeout_s 或缩小范围）。以下是超时前已产生的输出：")
    parts.append(f"命令：{command}")
    exit_code = "未知" if result.exit_code is None else str(result.exit_code)
    parts.append(f"退出码：{exit_code}，耗时：{_format_duration(result.duration_ms)}")
    parts.append("标准输出：")
    parts.append(stdout_text.strip() or "（无输出）")

    if stderr_text.strip():
        parts.append("标准错误：")
        parts.append(stderr_text.strip())

    if stdout_omitted or stderr_omitted:
        parts.append("（输出已截断，需要更多内容请用 grep / head / tail 缩小范围。）")
    return "\n".join(parts)


def _unusable_reason(lease: LeaseView) -> str | None:
    """租约存在但还不能用时的文案，能用时返回 None。
    `get_active_lease` 也会返回 `provisioning`（并发建机的后到者）；「正在启动」与「已回收」的
    下一步不同：前者该等几秒再试，后者会触发一次重建。
    """
    if lease.provider_ref:
        return None
    if lease.state == STATE_PROVISIONING:
        return "执行环境正在启动，请稍后再试。"
    return ERR_NOT_FOUND


# ---------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------
def _workspace_path(path: str | None) -> str | None:
    """校验路径，返回原样的（未归一化）绝对路径；不合法返回 None。
    只接受工作目录下的绝对路径且拒绝 `..`：猜模型意图比直接拒绝危险，猜错会读到无关的宿主内容；
    `..` 按路径段判断而不是子串匹配（`a..b.txt` 是合法文件名）。
    """
    raw = (path or "").strip()
    workdir = settings.sandbox_workdir.rstrip("/")
    if not raw or not raw.startswith("/"):
        return None
    if ".." in raw.split("/"):
        return None
    if not raw.startswith(f"{workdir}/"):
        return None
    return raw


def _rejected_path_message(path: str | None) -> str:
    workdir = settings.sandbox_workdir
    shown = (path or "").strip()[:200]
    return (
        f"路径不可用：{shown!r}。只允许执行环境工作目录 {workdir} 下的绝对路径，且不能包含「..」。"
        "未执行任何操作；请改用工作目录下的绝对路径重试。"
    )


def _clamp_timeout(timeout_s: int | None) -> int:
    """把模型给的超时夹到 `[1, MAX_EXEC_TIMEOUT_S]`；夹紧而不是拒绝，越界值多是模型随手写的大数。"""
    try:
        value = int(timeout_s) if timeout_s is not None else DEFAULT_EXEC_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_EXEC_TIMEOUT_S
    return max(1, min(value, MAX_EXEC_TIMEOUT_S))


def _clamp_max_bytes(max_bytes: int | None) -> int:
    """读取上限夹到 `[1, MAX_READ_BYTES]`。理由同上：拒绝不如夹紧，而上限本身是成本控制。"""
    try:
        value = int(max_bytes) if max_bytes is not None else MAX_READ_BYTES
    except (TypeError, ValueError):
        return MAX_READ_BYTES
    return max(1, min(value, MAX_READ_BYTES))


def _error_message(exc: SandboxError) -> str:
    """异常 → 给模型看的中文，不透传 `str(exc)`（含容器名 / sandbox id，且文案会随后端变化）。
    `SandboxNotFound` 不能细化：「不存在」与「不属于你」必须长得一样，否则这工具就是存在性探测器。
    """
    if isinstance(exc, SandboxNotFound):
        return ERR_NOT_FOUND
    if isinstance(exc, SandboxTimeout):
        return ERR_TIMEOUT
    if isinstance(exc, SandboxCapacityError):
        # 池满与「另一路正在建机」共用这一句：对模型来说下一步都是「稍后再试」。
        return ERR_CAPACITY
    return ERR_GENERIC


# ---------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------
async def sandbox_exec(ctx: RunContext[None], command: str, timeout_s: int = DEFAULT_EXEC_TIMEOUT_S) -> str:
    """在当前会话的隔离执行环境里运行一条 shell 命令。
    工作目录 /home/sandbox/workspace，文件在本会话内一直存在（重置或回收后消失）；timeout_s 默认 120、
    上限 900；输出截断保留头 4000 + 尾 2000 字符，要更多内容用 grep / head / tail 缩小范围而不是重跑。
    """
    scope = current_sandbox_scope()
    if scope is None:
        return NO_SCOPE_MESSAGE

    cleaned = (command or "").strip()
    if not cleaned:
        # 空命令会在沙箱里建一台机器然后什么也不做：第一次调用就白白付一次冷启动。
        return "命令为空，没有执行。请给出要执行的具体命令。"

    owner = owner_tag(scope.user_id)
    try:
        # provider 构造也在 try 里：后端配置写错抛的是 `SandboxError`，不该以异常形式漏给模型。
        provider = get_provider()
        lease = await lease_service.ensure_for_scope(user_id=scope.user_id, session_id=scope.session_id)
        unusable = _unusable_reason(lease)
        if unusable:
            return unusable
        result = await provider.exec(
            lease.provider_ref,
            cleaned,
            timeout_s=_clamp_timeout(timeout_s),
            workdir=settings.sandbox_workdir,
            owner_tag=owner,
        )
    except SandboxError as exc:
        logger.info("沙箱命令执行失败 (user=%s): %s", scope.user_id, type(exc).__name__)
        return _error_message(exc)
    except Exception:  # noqa: BLE001 - 工具契约是「永远返回文案，不把异常抛给模型」
        logger.exception("沙箱命令执行出现未预期异常 (user=%s)", scope.user_id)
        return ERR_GENERIC

    stdout_head, stdout_omitted = _truncate(
        result.stdout, head_chars=MAX_OUTPUT_HEAD_CHARS, tail_chars=MAX_OUTPUT_TAIL_CHARS
    )
    _, stderr_omitted = _truncate(result.stderr, head_chars=STDERR_HEAD_CHARS, tail_chars=STDERR_TAIL_CHARS)
    await lease_service.record_command(
        lease.id,
        command=cleaned,
        result=result,
        output_head=stdout_head,
        truncated=bool(stdout_omitted or stderr_omitted),
    )
    return render_exec_result(result, cleaned)


async def sandbox_write_file(ctx: RunContext[None], path: str, content: str) -> str:
    """把 UTF-8 文本写入执行环境里的一个文件（自动创建父目录），本会话内一直存在。
    path 必须是 /home/sandbox/workspace 下的绝对路径；content 上限 200000 字符，超限直接拒绝
    （不静默截断：半截文件要到下次执行才炸），拆成多段写入。写好的文件可以 python3 执行。
    """
    scope = current_sandbox_scope()
    if scope is None:
        return NO_SCOPE_MESSAGE

    target = _workspace_path(path)
    if target is None:
        return _rejected_path_message(path)

    body = content if content is not None else ""
    if len(body) > MAX_WRITE_CHARS:
        return (
            f"内容 {len(body):,} 字符超过单次写入上限 {MAX_WRITE_CHARS:,} 字符，未写入。"
            "请拆成多个文件或分段写入（例如先写主体，再用 sandbox_exec 追加），不要重发同一份内容。"
        )

    try:
        provider = get_provider()
        lease = await lease_service.ensure_for_scope(user_id=scope.user_id, session_id=scope.session_id)
        unusable = _unusable_reason(lease)
        if unusable:
            return unusable
        data = body.encode("utf-8")
        await provider.put_file(lease.provider_ref, target, data, owner_tag=owner_tag(scope.user_id))
    except SandboxError as exc:
        logger.info("沙箱写文件失败 (user=%s): %s", scope.user_id, type(exc).__name__)
        return _error_message(exc)
    except Exception:  # noqa: BLE001
        logger.exception("沙箱写文件出现未预期异常 (user=%s)", scope.user_id)
        return ERR_GENERIC

    await lease_service.touch(lease.id)
    return f"已写入 {target}（{len(data):,} 字节）。"


async def sandbox_read_file(ctx: RunContext[None], path: str, max_bytes: int = MAX_READ_BYTES) -> str:
    """读取执行环境里的一个文本文件（二进制内容会被替换成替换字符，没有阅读价值）。
    path 必须是 /home/sandbox/workspace 下的绝对路径；max_bytes 默认且上限 200000，更大的文件
    先用 sandbox_exec 的 wc -c / grep / head 看大小与片段，不要靠调大它。
    """
    scope = current_sandbox_scope()
    if scope is None:
        return NO_SCOPE_MESSAGE

    target = _workspace_path(path)
    if target is None:
        return _rejected_path_message(path)

    limit = _clamp_max_bytes(max_bytes)
    try:
        provider = get_provider()
        lease = await lease_service.ensure_for_scope(user_id=scope.user_id, session_id=scope.session_id)
        unusable = _unusable_reason(lease)
        if unusable:
            return unusable
        data = await provider.get_file(lease.provider_ref, target, max_bytes=limit, owner_tag=owner_tag(scope.user_id))
    except SandboxError as exc:
        logger.info("沙箱读文件失败 (user=%s): %s", scope.user_id, type(exc).__name__)
        return _error_message(exc)
    except Exception:  # noqa: BLE001
        logger.exception("沙箱读文件出现未预期异常 (user=%s)", scope.user_id)
        return ERR_GENERIC

    await lease_service.touch(lease.id)
    try:
        text = data.decode("utf-8")
        note = ""
    except UnicodeDecodeError:
        # 用替换字符读出来仍带「这不是文本」这个信息，直接报错反而让模型不知道文件里是什么。
        text = data.decode("utf-8", errors="replace")
        note = "（文件不是纯 UTF-8 文本，无法解码的字节已替换为 �）\n"
    return f"{target}（{len(data):,} 字节）：\n{note}{text}"


async def sandbox_reset(ctx: RunContext[None]) -> str:
    """扔掉当前会话的执行环境（文件、安装的包、进程全丢），下次执行时重建。
    用户说「重来一次」或环境被搞乱时调用；不当场重建新机器，连续两次重置才不会白开两台。
    """
    scope = current_sandbox_scope()
    if scope is None:
        return NO_SCOPE_MESSAGE

    try:
        provider = get_provider()
        lease, won = await lease_service.claim_for_reset(user_id=scope.user_id)
    except Exception:  # noqa: BLE001 - 工具契约是「永远返回文案，不把异常抛给模型」
        logger.exception("沙箱重置时读取租约失败 (user=%s)", scope.user_id)
        return ERR_GENERIC

    if lease is None:
        return "当前会话还没有创建执行环境，无需重置。下次执行时会自动创建。"

    if not won:
        # 别人（空闲回收 / 并发的另一次重置）已拿到销毁权，或机器还在建：不能声称「已重置」。
        return "当前执行环境正在创建或回收中，本次没有执行重置。请稍后重试；下一次执行会使用或创建环境。"

    try:
        if lease.provider_ref:
            await provider.destroy(lease.provider_ref, owner_tag=owner_tag(scope.user_id))
    except SandboxNotFound:
        # 销毁要求幂等：机器已经没了就是成功，不该让用户看到一次失败。
        pass
    except Exception as exc:  # noqa: BLE001
        # 停在 `destroying` 的租约既不被回收循环重试、也不是活动租约（见 lease.py），是最容易
        # 永久泄漏机器的中间态；失败必须落成 `destroy_failed`，告警与重试才有东西可查。
        logger.warning("沙箱销毁失败 (lease=%s): %s", lease.id, type(exc).__name__)
        await lease_service.mark_failed(lease.id, exc)
        return _error_message(exc) if isinstance(exc, SandboxError) else ERR_GENERIC

    await lease_service.finish_reset(lease.id)
    return "已重置，下次执行会创建新环境。"


__all__ = [
    "MAX_READ_BYTES",
    "MAX_WRITE_CHARS",
    "NO_SCOPE_MESSAGE",
    "render_exec_result",
    "sandbox_exec",
    "sandbox_read_file",
    "sandbox_reset",
    "sandbox_write_file",
]

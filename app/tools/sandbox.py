"""沙箱工具：让模型在一台**可丢弃**的隔离环境里跑命令、读写文件。

## 工具签名里为什么没有任何 id

四个工具都不收 `sandbox_id`（也不收 `user_id` / `session_id`）。租约由服务端按
`current_sandbox_scope()` 的 `(user_id, session_id)` 解析。

这不是「少了一个参数」，而是**授权形态的选择**：只要契约里有一个可以被模型填写的
句柄，就必须在每个入口都写一遍「这个句柄属于当前用户吗」，而漏写一处就是一次越权。
把句柄从契约里删掉之后，模型手里没有可伪造的输入，**越权在构造上不可达**——

- 换别人的 `lease_id`：不存在这个参数；
- 冒用别人的会话：`user_id` 来自登录态，租约表的每条查询都带它；
- 探测别人有没有环境：所有失败都回同一句「执行环境不存在或已回收」，不给存在性 oracle。

## 为什么四个函数不是 @registry.register 装饰器

注册需要风险等级、超时、启用开关这些装配信息，那是 `app/tools/__init__.py` 的职责。
这里只写「工具做什么」，装配与风险分级（exec/write/read = `READ_ONLY`，reset =
`SAFE_WRITE`）留在注册处，改装配不用读业务逻辑。

## 为什么都返回 str 而不是抛异常

模型看到的是文本：一句「执行环境暂时不可用，请稍后再试」能让它换个办法继续，一段
英文异常栈只会让它向用户复述 traceback，或者更糟——把「工具坏了」当成「任务失败了」。
执行环境的失败几乎都是瞬时或可规避的，所以只在这里翻译，不回传内部细节
（provider 的 ref、容器名、异常类型一律不出现在文案里）。
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from app.config import settings
from app.db import async_session_maker
from app.services.sandbox import get_provider
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
from app.services.sandbox.lease import (
    DESTROY_USER_RESET,
    STATE_PROVISIONING,
    LeaseView,
    ensure_lease,
    get_active_lease,
    mark_destroy_failed,
    mark_destroyed,
    mark_destroying,
    record_command,
    touch_activity,
)
from app.tools.sandbox_scope import SandboxScope, current_sandbox_scope

logger = logging.getLogger(__name__)

#: 没有作用域时的统一口径。与 `knowledge_search` 的「当前会话没有关联知识库」一致：
#: CLI 与未登录确实没有身份，说清楚比假装有一个默认身份好（后者会让所有匿名调用
#: 挤到同一台机器上）。
NO_SCOPE_MESSAGE = (
    "当前会话没有关联用户（CLI 模式或未登录），无法使用执行环境。请直接依据对话内容回答，并说明你没有可用的执行环境。"
)

#: 三种资源类失败的文案。分开写而不是共用一句，是因为模型该采取的下一步不同：
#: 池满该等、超时该缩小范围、不存在该重试（重试会重建）。
ERR_CAPACITY = "当前执行环境资源已满，请稍后再试。"
ERR_NOT_FOUND = "执行环境不存在或已回收，请重试；下次执行会自动创建新环境。"
ERR_TIMEOUT = "命令超时（可调大 timeout_s 或缩小范围）。"
ERR_GENERIC = "执行环境暂时不可用，请稍后再试。"

#: 单次写入的字符上限。超限**拒绝**而不是静默截断：半截脚本会以「语法错误」或更隐蔽
#: 的方式在下一次执行时爆炸，而模型不知道自己写进去的是半截。
MAX_WRITE_CHARS = 200_000

#: 单次读取的字节上限（也是 `sandbox_read_file` 的 `max_bytes` 天花板）。整份大文件
#: 进上下文就是一次 token 事故，而「读一部分」永远可以退化成 `head` / `grep`。
MAX_READ_BYTES = 200_000

#: stderr 的截断阈值。比 stdout 短：正常命令的 stderr 只有几行，出现几万行 stderr 时
#: 重复的报错行对模型没有任何新信息。
STDERR_HEAD_CHARS = 1_500
STDERR_TAIL_CHARS = 1_500


# ---------------------------------------------------------------------
# 渲染（纯函数，可单测）
# ---------------------------------------------------------------------
def _truncate(text: str, *, head_chars: int, tail_chars: int) -> tuple[str, int]:
    """保留头尾、中间标注省略了多少字符。返回 `(展示文本, 省略字符数)`。

    保留**尾部**而不只是头部：命令的关键信息常常在最后（`pip install` 的结论、
    报错的最后一行、`tail` 的输出），只截头会让模型看到 4000 个进度条字符。
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
    """把一次执行结果渲染成给模型看的文本。

    纯函数、单独导出：截断规则是本项目的**成本控制点**
    （——沙箱的花费只通过「exec 输出进上下文」这一条通路变成 token），所以它必须能被
    单独测，而不是只能靠跑一条真命令来观察。

    超时时明确写「命令超时（可调大 timeout_s 或缩小范围）」而不是「环境坏了」：后者会
    让模型去重建沙箱——重来一遍仍然是同一条超时的命令，只是多花一次冷启动。
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


# ---------------------------------------------------------------------
# 租约：按作用域解析，必要时隐式创建
# ---------------------------------------------------------------------
async def _ensure_lease(scope: SandboxScope) -> LeaseView:
    """取本会话的租约；还没有机器就建一台。

    创建交给 `ensure_lease`，因为幂等点是 `sandbox_lease` 上的部分唯一索引，不是这里的
    一段判断——同一轮里模型并发调两个工具是真实存在的，只有那行 `provisioning` 能保证
    它们拿到同一台机器。`SandboxSpec` 也由租约层拼（行 id 与 provider 侧 label 必须同一个
    值），所以这里直接把 `provider.create` 递过去，不在工具层再抄一份配置映射。

    建机期间确实会持有这条数据库连接（冷启动几十秒）：这是「先落库再建机」的固有代价，
    而 `ensure_lease` 在调 `create` 之前已经提交过了，所以没有行锁被按那么久。除了建机，
    工具层不在任何 provider 调用期间持有数据库会话——`exec` 可能要跑几分钟，攥着连接
    等它等于把连接池按并发命令数耗尽。
    """
    async with async_session_maker() as session:
        lease, _created = await ensure_lease(
            session,
            user_id=scope.user_id,
            session_id=scope.session_id,
            create=get_provider().create,
        )
    return lease


def _unusable_reason(lease: LeaseView) -> str | None:
    """租约存在但还不能用时的文案；能用时返回 None。

    `get_active_lease` 会把 `provisioning` 也返回：并发建机时后到的那一路读到的就是
    这台还没起来的机器（`provider_ref` 仍为空）。把「正在启动」与「已回收」分开说，
    是因为两者的下一步不同：前者该等几秒再试，后者会触发一次重建。
    """
    if lease.provider_ref:
        return None
    if lease.state == STATE_PROVISIONING:
        return "执行环境正在启动，请稍后再试。"
    return ERR_NOT_FOUND


async def _touch_activity(lease_id: str) -> None:
    """刷新 `last_activity`：证明机器还在被用（回收循环靠它区分「用户关了页面」与
    「agent 还在跑长任务」）。

    失败只记日志、不影响工具结果：命令已经执行完了，因为一次统计写入失败就告诉模型
    「环境不可用」是错的。代价是这台机器可能被空闲回收早一点带走——下一轮会重建。
    """
    try:
        async with async_session_maker() as session:
            await touch_activity(session, lease_id)
    except Exception:  # noqa: BLE001 - 见 docstring：统计失败不该改变工具结果
        logger.exception("刷新沙箱活动时间失败 (lease=%s)", lease_id)


async def _record_command(
    lease: LeaseView, command: str, result: ExecResult, output_head: str, truncated: bool
) -> None:
    """把这次执行写进审计表（`sandbox_command`）。

    与 `_touch_activity` 共用一条连接（两条语句各自提交，见 `lease.py`——它保留提交权，
    因为状态迁移的条件 UPDATE 必须立刻对其他回收者可见）。失败同样只记日志：审计缺失要
    告警，但不能让模型以为命令没跑。
    """
    try:
        async with async_session_maker() as session:
            await touch_activity(session, lease.id)
            await record_command(
                session,
                lease.id,
                command=command,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                output_bytes=result.output_bytes,
                # 审计要回答的是「模型当时看到了什么」，所以存的是渲染后的截断文本
                # （见 `app/db.py` 里 SandboxCommand 的说明），不是原始输出。
                output_head=output_head,
                truncated=truncated,
            )
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.exception("写入沙箱命令审计失败 (lease=%s)", lease.id)


# ---------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------
def _workspace_path(path: str | None) -> str | None:
    """校验路径并返回原样（未归一化的）绝对路径；不合法时返回 None。

    只接受工作目录下的绝对路径，且拒绝任何 `..` 路径段：沙箱里确实还有 `/etc`、`/proc`
    这些位置，而模型把「工作目录」理解错的时候，猜它的意图比直接拒绝危险得多
    ——拒绝一次只需重试一条命令，猜错一次会读到与用户问题无关的宿主/模板内容。

    `..` 按路径段判断（`raw.split("/")`）而不是子串匹配：`a..b.txt` 是合法文件名。
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
    """把模型给的超时夹到 `[1, MAX_EXEC_TIMEOUT_S]`。

    夹紧而不是拒绝：越界的超时值通常是模型随手写的大数（`3600`），拒绝等于白烧一轮
    工具调用，而夹到上限后仍是一次有意义的执行。
    """
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
    """异常 → 给模型看的中文。

    **不透传 `str(exc)`**：provider 与租约层的消息可能带容器名、sandbox id、英文原文，
    那些既是内部细节，也会让同一种失败在不同后端下变成不同的文案（模型会以为它们
    是不同的问题）。`SandboxNotFound` 尤其不能细化——「不存在」与「不属于你」必须
    长得一模一样，否则这个工具就是一个存在性探测器。
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

    适用：需要真正执行代码来回答的问题——跑一段 Python 验证结论、处理用户给的数据、
    生成图表、装依赖试库版本。环境里没有网络出站以外的宿主资源，工作目录是
    /home/sandbox/workspace，里面的文件在本会话内持续存在（重置或回收后消失）。

    参数：
    - command：要执行的 shell 命令。一条命令做一件小事比一条长流水线更好排障。
    - timeout_s：超时秒数，默认 120，上限 900。超时会返回「命令超时」以及已产生的输出。

    输出会被截断（保留头 4000 + 尾 2000 字符）。需要更多内容时不要重跑大输出命令，
    改用 grep / head / tail 缩小范围——每一次大输出都会把本轮对话的 token 成本推高。
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
        # provider 的构造也在 try 里：后端配置写错（未知 backend）会抛 `SandboxError`，
        # 那同样是「环境不可用」，不该以异常形式漏给模型。
        provider = get_provider()
        lease = await _ensure_lease(scope)
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
    await _record_command(lease, cleaned, result, stdout_head, bool(stdout_omitted or stderr_omitted))
    return render_exec_result(result, cleaned)


async def sandbox_write_file(ctx: RunContext[None], path: str, content: str) -> str:
    """把文本写入执行环境里的一个文件（会创建父目录）。

    参数：
    - path：沙箱内的**绝对**路径，必须在 /home/sandbox/workspace 之下，例如
      /home/sandbox/workspace/analysis.py。
    - content：文件内容（UTF-8 文本，最多 200000 字符）。

    写好的文件可以用 sandbox_exec 执行（python3 analysis.py），本会话内一直存在。
    写不进去时明确返回原因（路径不合法 / 内容超长 / 环境不可用），不会静默截断内容。
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
        lease = await _ensure_lease(scope)
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

    await _touch_activity(lease.id)
    return f"已写入 {target}（{len(data):,} 字节）。"


async def sandbox_read_file(ctx: RunContext[None], path: str, max_bytes: int = MAX_READ_BYTES) -> str:
    """读取执行环境里的一个文件。

    参数：
    - path：沙箱内的**绝对**路径，必须在 /home/sandbox/workspace 之下。
    - max_bytes：读取上限（默认且最大 200000 字节）。更大的文件不要靠调大它来读，
      先用 sandbox_exec 里的 wc -c / grep / head 查看大小与片段。

    只适用于文本文件：二进制内容（图片、压缩包）会被替换成替换字符，没有阅读价值。
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
        lease = await _ensure_lease(scope)
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

    await _touch_activity(lease.id)
    try:
        text = data.decode("utf-8")
        note = ""
    except UnicodeDecodeError:
        # 二进制文件用替换字符读出来仍有「这不是文本」这个信息，直接报错反而让模型
        # 不知道文件里到底是什么。
        text = data.decode("utf-8", errors="replace")
        note = "（文件不是纯 UTF-8 文本，无法解码的字节已替换为 �）\n"
    return f"{target}（{len(data):,} 字节）：\n{note}{text}"


async def _mark_destroy_failed(lease_id: str, exc: BaseException) -> None:
    """把销毁失败落成 `destroy_failed`（带原因），失败本身只记日志。

    这一步失败也不能改变给模型的回答：机器确实可能还在，但模型没有「重试销毁」的手段，
    让它知道细节只会多一轮无用的调用；运维侧靠日志与那条 `destroying` 行排查。
    """
    try:
        async with async_session_maker() as session:
            await mark_destroy_failed(
                session,
                lease_id,
                error=f"{type(exc).__name__}: {exc}",
                reason=DESTROY_USER_RESET,
            )
    except Exception:  # noqa: BLE001
        logger.exception("写入沙箱销毁失败状态时出错 (lease=%s)", lease_id)


async def sandbox_reset(ctx: RunContext[None]) -> str:
    """扔掉当前会话的执行环境，下次执行时重建。

    用户说「重来一次」「环境搞乱了」时调用。里面的一切（文件、安装的包、跑着的进程）
    都会消失，且**不会**当场重建新机器——重建发生在下一次 sandbox_exec 等工具的调用里，
    这样连续两次重置不会白开两台机器。
    """
    scope = current_sandbox_scope()
    if scope is None:
        return NO_SCOPE_MESSAGE

    try:
        provider = get_provider()
        async with async_session_maker() as session:
            lease = await get_active_lease(session, user_id=scope.user_id)
            if lease is None:
                return "当前会话还没有创建执行环境，无需重置。下次执行时会自动创建。"
            # 条件 UPDATE 抢占销毁权：回收循环可能同时在场，先读后写会把机器从长任务下面抽走。
            won = await mark_destroying(session, lease.id)
    except Exception:  # noqa: BLE001 - 工具契约是「永远返回文案，不把异常抛给模型」
        logger.exception("沙箱重置时读取租约失败 (user=%s)", scope.user_id)
        return ERR_GENERIC

    if not won:
        # 别人（空闲回收 / 并发的另一次重置）已经拿到销毁权，或者这台机器还在建。
        # 两种情况下本次都没有真的重置，所以不声称「已重置」。
        return "当前执行环境正在创建或回收中，本次没有执行重置。请稍后重试；下一次执行会使用或创建环境。"

    try:
        if lease.provider_ref:
            await provider.destroy(lease.provider_ref, owner_tag=owner_tag(scope.user_id))
    except SandboxNotFound:
        # 销毁要求幂等：机器已经没了就是成功，不该让用户看到一次失败。
        pass
    except Exception as exc:  # noqa: BLE001
        # 停在 `destroying` 的租约既不会被回收循环重试、也不再是活动租约（回收候选只看
        # ready/paused，见 lease.py）——那是最容易永久泄漏机器的中间态。所以失败必须落成
        # `destroy_failed` 才走，告警与重试才有东西可查。
        logger.warning("沙箱销毁失败 (lease=%s): %s", lease.id, type(exc).__name__)
        await _mark_destroy_failed(lease.id, exc)
        return _error_message(exc) if isinstance(exc, SandboxError) else ERR_GENERIC

    try:
        async with async_session_maker() as session:
            await mark_destroyed(session, lease.id, reason=DESTROY_USER_RESET)
    except Exception:  # noqa: BLE001
        # 机器已经销毁，只是账没有对上：回收循环会按 destroying 重试，用户看到的结果
        # （下一次执行是新环境）不受影响，所以不把它报成失败。
        logger.exception("沙箱已销毁但租约状态未落库 (lease=%s)", lease.id)
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

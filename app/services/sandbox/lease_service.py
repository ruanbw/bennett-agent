"""沙箱租约编排：一次工具调用到底需要哪几次 lease 写操作，以及哪些失败只记日志。

数据库会话留在这里而不上浮到工具层：exec 可能跑几分钟，握着连接会耗尽池；工具层只表达
「取租约 / 记一次执行 / 标记失败」这类业务语义，不关心会话怎么开、何时提交。
"""

from __future__ import annotations

import logging
import uuid

from app.db import async_session_maker
from app.services.sandbox import get_provider
from app.services.sandbox import lease as lease_repo
from app.services.sandbox.base import ExecResult
from app.services.sandbox.lease import DESTROY_USER_RESET, LeaseView

logger = logging.getLogger(__name__)


async def ensure_for_scope(*, user_id: uuid.UUID, session_id: str) -> LeaseView:
    """取这个会话的租约，没有就建一台。
    幂等点在 `sandbox_lease` 的部分唯一索引（`ensure_lease`）；会话何时开、何时提交留在这里，
    工具层不必知道。
    """
    async with async_session_maker() as session:
        lease, _created = await lease_repo.ensure_lease(
            session,
            user_id=user_id,
            session_id=session_id,
            create=get_provider().create,
        )
    return lease


async def touch(lease_id: str) -> None:
    """刷新 `last_activity`，回收循环靠它区分「用户关了页面」与「agent 还在跑长任务」。
    失败只记日志：命令已经跑完，统计写入失败不该变成「环境不可用」，代价是可能被回收早一点。
    """
    try:
        async with async_session_maker() as session:
            await lease_repo.touch_activity(session, lease_id)
    except Exception:  # noqa: BLE001 - 见 docstring：统计失败不该改变工具结果
        logger.exception("刷新沙箱活动时间失败 (lease=%s)", lease_id)


async def record_command(
    lease_id: str,
    *,
    command: str,
    result: ExecResult,
    output_head: str,
    truncated: bool,
) -> None:
    """把这次执行写进审计表（顺带刷新活动时间），失败只记日志。
    审计缺失要告警，但不能让模型以为命令没跑；提交权在 `lease.py`：状态迁移的条件 UPDATE
    必须立刻对其他回收者可见。
    """
    try:
        async with async_session_maker() as session:
            await lease_repo.touch_activity(session, lease_id)
            await lease_repo.record_command(
                session,
                lease_id,
                command=command,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                output_bytes=result.output_bytes,
                # 审计要回答「模型当时看到了什么」，所以存渲染后的截断文本（见 `app/db.py`）。
                output_head=output_head,
                truncated=truncated,
            )
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.exception("写入沙箱命令审计失败 (lease=%s)", lease_id)


async def claim_for_reset(*, user_id: uuid.UUID) -> tuple[LeaseView | None, bool]:
    """抢占这个用户当前租约的销毁权，返回 `(租约, 是否抢到)`。
    `lease is None` 是「还没有环境」、`won=False` 是「正在建或在被回收」，调用方必须分开说话；
    只有真抢到才能销毁机器。这里不吞异常：读不到租约时工具层该回统一失败文案。
    """
    async with async_session_maker() as session:
        lease = await lease_repo.get_active_lease(session, user_id=user_id)
        if lease is None:
            return None, False
        # 条件 UPDATE 抢占销毁权：回收循环可能同时在场，先读后写会把机器从长任务下面抽走。
        return lease, await lease_repo.mark_destroying(session, lease.id)


async def mark_failed(lease_id: str, exc: BaseException) -> None:
    """把销毁失败落成 `destroy_failed`（带原因），这一步失败只记日志。
    模型没有「重试销毁」的手段，报细节只会多一轮无用调用；运维靠日志与那条 `destroying` 行排查。
    """
    try:
        async with async_session_maker() as session:
            await lease_repo.mark_destroy_failed(
                session,
                lease_id,
                error=f"{type(exc).__name__}: {exc}",
                reason=DESTROY_USER_RESET,
            )
    except Exception:  # noqa: BLE001
        logger.exception("写入沙箱销毁失败状态时出错 (lease=%s)", lease_id)


async def finish_reset(lease_id: str) -> None:
    """`destroying → destroyed`；失败只记日志。
    机器已销毁、只是账没对上：回收循环会按 destroying 重试，所以不报成失败。
    """
    try:
        async with async_session_maker() as session:
            await lease_repo.mark_destroyed(session, lease_id, reason=DESTROY_USER_RESET)
    except Exception:  # noqa: BLE001
        logger.exception("沙箱已销毁但租约状态未落库 (lease=%s)", lease_id)


__all__ = [
    "claim_for_reset",
    "ensure_for_scope",
    "finish_reset",
    "mark_failed",
    "record_command",
    "touch",
]

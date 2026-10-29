"""Redis 会话热缓存：`access_token` 摘要 → 行内容的 cache-aside 读写与失效。

刻意与 `models/user.py` 单向依赖：本模块只在函数内 import `AccessToken`（构造缓存命中行），
否则 `models/user.py` 要调用本模块、本模块又在模块层 import 它，形成循环。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.db.models.user import AccessToken

logger = logging.getLogger(__name__)


async def _session_cache_get(hashed_token: str) -> AccessToken | None:
    """从 Redis 取会话行；未启用/未命中/任何故障都返回 None，即回源数据库。

    刻意不抛异常：认证路径上多一个可失败的依赖，等于把 Redis 抖动升级成全站 401。
    """
    if not settings.redis_session_cache_enabled:
        return None
    from app.services.redis_client import get_redis

    try:
        raw = await get_redis().get(f"{settings.redis_key_prefix}:session:{hashed_token}")
    except Exception:  # noqa: BLE001 - 缓存故障绝不阻塞认证
        logger.warning("读取会话缓存失败，回源数据库", exc_info=True)
        return None
    if not raw:
        return None
    from app.db.models.user import AccessToken

    try:
        payload = json.loads(raw)
        return AccessToken(
            token=hashed_token,
            user_id=uuid.UUID(payload["user_id"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
        )
    except (ValueError, KeyError, TypeError):
        # 结构不认识（版本升级/别的写者）：当作 miss，并让键尽快过期
        logger.warning("会话缓存内容无法解析，已忽略")
        return None


async def _session_cache_set(hashed_token: str, row) -> None:  # noqa: ANN001
    """把会话行写进 Redis，TTL 取「缓存上限」与「会话剩余寿命」的较小值。

    缓存不能比会话本身活得更久，否则已过期的会话还能从缓存里被取出来。
    """
    if not settings.redis_session_cache_enabled:
        return
    ttl = _session_cache_ttl(row.created_at)
    if ttl <= 0:
        return
    from app.services.redis_client import get_redis

    payload = json.dumps(
        {"user_id": str(row.user_id), "created_at": row.created_at.isoformat()},
        separators=(",", ":"),
    )
    try:
        await get_redis().set(
            f"{settings.redis_key_prefix}:session:{hashed_token}",
            payload,
            ex=ttl,
        )
    except Exception:  # noqa: BLE001 - 回填失败只影响下一次读的性能
        logger.warning("写入会话缓存失败（不影响本次请求）", exc_info=True)


async def _session_cache_delete(hashed_token: str) -> None:
    """删除会话缓存。失败只告警：库已经是权威状态，缓存会随 TTL 过期。"""
    if not settings.redis_session_cache_enabled:
        return
    from app.services.redis_client import get_redis

    try:
        await get_redis().delete(f"{settings.redis_key_prefix}:session:{hashed_token}")
    except Exception:  # noqa: BLE001
        logger.warning("删除会话缓存失败（该会话最迟在缓存 TTL 后失效）", exc_info=True)


def _session_cache_ttl(created_at: datetime) -> int:
    """缓存剩余有效秒数：min(配置上限, 会话剩余寿命)。"""
    lifetime = settings.session_lifetime_seconds
    if lifetime <= 0:
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    remaining = int((created_at + timedelta(seconds=lifetime) - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(int(settings.redis_session_cache_ttl_seconds), remaining))

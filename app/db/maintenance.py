"""维护动作：过期/按用户清理会话行，以及 API Key 解密审计。"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text

from app.config.secret_box import PREFIX, SecretDecryptError, decrypt_secret
from app.db.base import async_session_maker
from app.db.models.user import AccessToken
from app.db.session_cache import _session_cache_delete

logger = logging.getLogger(__name__)


async def audit_provider_key_decryption() -> list[str]:
    """逐条试解 `model_provider.api_key`，返回解不开的提供商 id（十六进制字符串）。

    必须用原始 SQL：经过 `EncryptedText` 后拿到的已是解密结果，分不清「没配」与「解不开」。
    换 `AUTH_SECRET` 是允许的运维动作，其症状指向填错 key，所以要由启动日志与就绪探针说出真相。
    """
    async with async_session_maker() as session:
        rows = (await session.execute(text("SELECT id, api_key FROM model_provider"))).all()
    broken: list[str] = []
    for row in rows:
        raw = row[1]
        if not raw or not str(raw).startswith(PREFIX):
            continue
        try:
            decrypt_secret(str(raw))
        except SecretDecryptError:
            broken.append(str(row[0]))
    return broken


async def cleanup_expired_access_tokens(lifetime_seconds: int) -> int:
    """删除已过期的会话行，返回删除条数。

    `DatabaseStrategy.read_token` 只把 `created_at` 当过滤条件、从不删行，没有这个维护动作
    表会随登录次数无限增长。用独立会话：调用方通常正处于自己的事务里，这是无关的维护动作。
    """
    if lifetime_seconds <= 0:
        # 与 services/auth.py 的 get_database_strategy 同源：lifetime<=0 时 cutoff 落在现在或未来，
        # 会把全部有效会话当成过期删掉，比不清理危险得多。
        raise ValueError(
            f"cleanup_expired_access_tokens 的 lifetime_seconds 必须 > 0，当前为 {lifetime_seconds}："
            "0/负值会让服务端会话永不过期，按此清理会误删全部有效会话。"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lifetime_seconds)
    async with async_session_maker() as session:
        stale = (await session.execute(select(AccessToken.token).where(AccessToken.created_at < cutoff))).scalars()
        tokens = list(stale)
        await session.execute(delete(AccessToken).where(AccessToken.created_at < cutoff))
        await session.commit()

    # 缓存与库一起清：只删库时，已登出的会话还能从 Redis 里取出来用（最坏到 TTL）。
    for token in tokens:
        await _session_cache_delete(token)
    return len(tokens)


async def delete_user_access_tokens(user_id: uuid.UUID) -> int:
    """删除某个用户的全部会话行，返回删除条数（改密 / 踢人 / 停用账号用）。

    只按 `user_id` 过滤，否则改 A 的密码会把 B 踢下线。必须先读出摘要再删：缓存键就是摘要，
    不知道键名撤销就拖到一个 TTL 才生效。每用户几十条顶天，一次全取不成为负担。
    """
    async with async_session_maker() as session:
        tokens = list(
            (await session.execute(select(AccessToken.token).where(AccessToken.user_id == user_id))).scalars()
        )
        result = await session.execute(delete(AccessToken).where(AccessToken.user_id == user_id))
        await session.commit()

    for token in tokens:
        await _session_cache_delete(token)
    return result.rowcount or 0

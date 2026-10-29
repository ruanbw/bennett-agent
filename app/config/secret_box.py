"""敏感配置的落库加密（API Key at rest）：用 `AUTH_SECRET` 派生 Fernet 密钥做对称加密。
存储为 `enc:v1:<token>`，读取时透明解密；旧明文行原样返回（不需要迁移脚本）；`AUTH_SECRET` 更换后
旧密文解不开就抛错，而不是把密文当 key 发给上游（生产启动守卫已保证 secret 强随机）。
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

#: 密文前缀，用来区分历史明文行。
PREFIX = "enc:v1:"


def _fernet_for(secret: str) -> Fernet:
    """把任意长度的 AUTH_SECRET 派生为 Fernet 需要的 32 字节 urlsafe key。"""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


_fernet = _fernet_for(settings.auth_secret)


def encrypt_secret(value: str) -> str:
    return f"{PREFIX}{_fernet.encrypt(value.encode('utf-8')).decode('ascii')}"


class SecretDecryptError(Exception):
    """密文存在但解不开（AUTH_SECRET 被换过，或这一行本身已损坏）。"""


def decrypt_secret(value: str | None) -> str:
    """解密；解不开时抛 `SecretDecryptError` 而不是返回空串。
    空串会让 `_auth_headers` 干脆不发 Authorization，上游 401 被归因成「key 填错了」，实际根因是 `AUTH_SECRET` 换了。
    `ValueError` / `UnicodeError` 也要一起接住（非法 base64、`enc:v1:中文`），否则会从 ORM 冒出来让整个列表端点 500。
    """
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value  # 历史明文行：原样使用，下次保存时自然加密
    try:
        return _fernet.decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise SecretDecryptError(
            "敏感配置解密失败：AUTH_SECRET 可能已更换，请在管理后台重新填写该提供商的 API Key"
        ) from exc

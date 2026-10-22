"""敏感配置的落库加密（API Key at rest）。

数据库里的提供商 API Key 此前是明文：SQLite 文件被读（备份、误提交、同主机其他
用户）就等于拿到所有上游凭据。这里用 `AUTH_SECRET` 派生 Fernet 密钥做对称加密：

- 写入时 `enc:v1:<token>`；读取时透明解密；
- 旧明文值原样返回（兼容升级，不需要迁移脚本）；
- `AUTH_SECRET` 被更换后旧密文无法解密，函数记 ERROR 并返回空串（宁可让管理员
  重新填 key，也不要把密文当 key 发给上游）。

密钥来自 AUTH_SECRET：生产环境已有「必须是强随机 ≥32 字符」的启动守卫，因此
默认配置下这是强密钥；本地开发用的是占位值，只防误读、不防知道源码的人。
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


def decrypt_secret(value: str) -> str:
    if not value.startswith(PREFIX):
        return value  # 历史明文行：原样使用，下次保存时自然加密
    try:
        return _fernet.decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("敏感配置解密失败：AUTH_SECRET 可能已更换，请在管理后台重新填写该提供商的 API Key")
        return ""

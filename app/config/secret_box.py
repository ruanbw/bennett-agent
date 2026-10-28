"""敏感配置的落库加密（API Key at rest）。

数据库里的提供商 API Key 此前是明文：数据库备份被读（备份、误提交、同主机其他
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


class SecretDecryptError(Exception):
    """密文存在但解不开（AUTH_SECRET 被换过，或这一行本身已损坏）。"""


def decrypt_secret(value: str | None) -> str:
    """解密；**解不开时抛 `SecretDecryptError`，不返回空串**。

    ## 为什么不能返回空串

    以前这里 `except InvalidToken: return ""`。空串会一路静默下去：
    `_auth_headers` 看到 `provider.api_key` 为空就干脆**不发 Authorization**
    （`registry.py` 的 `if provider.api_key and ...`）→ 上游 401 → 用户看到
    「上游认证配置问题，请联系管理员」；后台的「密钥」列显示「未设置」，于是管理员
    看不出「有密文、但解不开」，只会去把 key 再填一遍。而根因是 `AUTH_SECRET` 换了。

    抛异常之后，密钥是**显式不可用**的：列表里那一行会带上 `api_key_error`，
    `/health/ready` 能报出「有几个提供商的密钥解不开」，日志里有 provider id。

    ## 另外两个必须一起接住的异常

    `InvalidToken` 只覆盖「格式对但解不开」。还有两类会让整个列表端点 500：
    - `UnicodeEncodeError`：列里出现 `enc:v1:中文`（dump/restore 编码错乱，或历史
      明文行恰好以 `enc:v1:` 开头）时 `.encode("ascii")` 直接抛，而它会从 ORM 的
      `process_result_value` 冒出来，让 `GET /api/admin/providers` 整体 500；
    - `ValueError`：Fernet 对不合法的 base64 抛的就是它。
    三者一律翻成 `SecretDecryptError`，调用方按「这一行不可用」处理。
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

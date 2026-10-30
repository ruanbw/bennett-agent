"""敏感配置的落库加密（API Key at rest）：用 `PROVIDER_KEY_SECRET`（缺省回退 `AUTH_SECRET`）派生 Fernet 密钥做对称加密。

密文形态 `enc:v2:<kid>:<token>`，`kid` 是密钥指纹前 8 位——它只回答「这一行是哪把密钥加密的」，
不含密钥本身。读取用 `MultiFernet([当前, 上一把])`：轮换密钥时不再等于把全部提供商凭据作废，
旧密文只要上一把密钥还配在 `PROVIDER_KEY_SECRET_PREVIOUS` 里就仍能解。
`enc:v1:<token>`（不带 key id 的历史格式）继续可解；更早的明文行原样返回（不需要迁移脚本）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)

#: 密文标记。`enc:v1:<token>`（旧格式，无 key id）与 `enc:v2:<kid>:<token>` 都以它开头，
#: 调用方（`app/db/maintenance.audit_provider_key_decryption`）靠它把密文行与历史明文行分开。
PREFIX = "enc:v"

#: 旧格式：版本后直接是 token。
_LEGACY_PREFIX = "enc:v1:"

#: 当前格式：版本 + key id + token。
_CURRENT_PREFIX = "enc:v2:"


def _key_material(secret: str) -> bytes:
    """把任意长度的密钥串派生为 Fernet 需要的 32 字节 urlsafe key。"""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _fernet_for(secret: str) -> Fernet:
    return Fernet(_key_material(secret))


def _kid_for(secret: str) -> str:
    """密钥指纹前 8 位：写进密文前缀，让「这一行还没重加密」可被肉眼与脚本识别。

    取的是派生后密钥的摘要而不是密钥本身；32 字节随机密钥的 8 位摘要无法反推密钥。
    """
    return hashlib.sha256(_key_material(secret)).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class _KeyRing:
    """当前密钥（唯一用于加密）与上一把（只用于解密轮换前的旧密文）。"""

    crypt: MultiFernet
    kid: str


def _build_key_ring(current_secret: str, previous_secret: str | None) -> _KeyRing:
    keys = [_fernet_for(current_secret)]
    if previous_secret:
        # 顺序有意义：`MultiFernet` 依序尝试，新密钥在前意味着日常解密只多一次失败尝试。
        keys.append(_fernet_for(previous_secret))
    return _KeyRing(crypt=MultiFernet(keys), kid=_kid_for(current_secret))


def _current_secret() -> str:
    """加密密钥：显式配置优先，否则回退 `AUTH_SECRET`（旧部署的默认行为）。"""
    return (settings.provider_key_secret or "").strip() or settings.auth_secret


_KEY_RING = _build_key_ring(_current_secret(), (settings.provider_key_secret_previous or "").strip() or None)

if not (settings.provider_key_secret or "").strip():
    # 只打一条 INFO，不影响启动：两把密钥共用是当前默认行为，但应该被主动决定而不是永久默认。
    logger.info(
        "PROVIDER_KEY_SECRET 未设置，提供商 API Key 的加密密钥回退为 AUTH_SECRET。"
        "建议分开设置：AUTH_SECRET 泄露（用于签名重置密码令牌）必须立刻轮换，"
        "而轮换它会同时让已加密的 API Key 解不开；独立设置后两者可以各自轮换。"
    )


def current_key_id() -> str:
    """当前加密密钥的 kid——新写出的密文前缀里就是它。"""
    return _KEY_RING.kid


def key_id_of(value: str | None) -> str | None:
    """读出密文里记录的 kid；明文行、旧 `enc:v1:` 格式与坏格式返回 None。"""
    if not value or not value.startswith(_CURRENT_PREFIX):
        return None
    kid, separator, _ = value[len(_CURRENT_PREFIX) :].partition(":")
    if not separator or not kid:
        return None
    return kid


def encrypt_secret(value: str) -> str:
    token = _KEY_RING.crypt.encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_CURRENT_PREFIX}{_KEY_RING.kid}:{token}"


class SecretDecryptError(Exception):
    """密文存在但解不开（密钥被换掉且没有保留上一把，或这一行本身已损坏）。"""


def decrypt_secret(value: str | None) -> str:
    """解密；解不开时抛 `SecretDecryptError` 而不是返回空串。
    空串会让 `build_auth_headers` 干脆不发 Authorization，上游 401 被归因成「key 填错了」，根因是密钥换了。
    `ValueError` / `UnicodeError` 也要一起接住（非法 base64、`enc:v1:中文`），否则会从 ORM 冒出来让整个列表端点 500。
    """
    if not value:
        return ""
    if value.startswith(_CURRENT_PREFIX):
        # kid 不参与解密：`MultiFernet` 自己按「当前 → 上一把」尝试，读 kid 只是为了日志与轮换报告。
        # 缺 kid 的畸形行也照旧尝试解密，不因为前缀瑕疵把一份可解的凭据判死。
        _, separator, token = value[len(_CURRENT_PREFIX) :].partition(":")
        if not separator:
            token = value[len(_CURRENT_PREFIX) :]
    elif value.startswith(_LEGACY_PREFIX):
        token = value[len(_LEGACY_PREFIX) :]
    else:
        return value  # 历史明文行：原样使用，下次保存时自然加密
    try:
        return _KEY_RING.crypt.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise SecretDecryptError(
            "敏感配置解密失败：加密密钥可能已更换且没有保留上一把（PROVIDER_KEY_SECRET_PREVIOUS），"
            "请在管理后台重新填写该提供商的 API Key"
        ) from exc

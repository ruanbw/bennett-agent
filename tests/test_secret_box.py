"""API Key 落库加密（I-06/I-10）的纯逻辑侧。

加密发生在 SQLAlchemy 的 bind/result 边界：**库里是密文、ORM 读出来是明文**。
这里只锁加密原语本身（往返、旧明文兼容、解不开必须显式报错）；bind/result 边界
上的落库形态需要真实数据库，已随「零基础设施」改造移出测试层。
"""

import pytest
from cryptography.fernet import Fernet

from app.config import secret_box


def test_secret_roundtrip_hides_plaintext():
    encrypted = secret_box.encrypt_secret("sk-super-secret")

    assert encrypted.startswith(secret_box.PREFIX)
    assert "sk-super-secret" not in encrypted
    assert secret_box.decrypt_secret(encrypted) == "sk-super-secret"


def test_legacy_plaintext_is_returned_unchanged():
    """升级前写进去的明文行必须还能用，不能要求停机迁移。"""
    assert secret_box.decrypt_secret("sk-legacy") == "sk-legacy"


def test_wrong_key_raises_instead_of_returning_empty(monkeypatch):
    """解不开时必须抛，不能返回空串。

    返回空串会一路静默：`build_auth_headers` 看到空 key 就不发 Authorization → 上游 401 →
    用户看到「上游认证配置问题」；后台显示「未设置」，管理员只会去重填 key。而根因是
    AUTH_SECRET 换过。异常让「这一行不可用」成为显式状态。
    """
    from app.config import secret_box as module

    encrypted = module.encrypt_secret("sk-secret")
    monkeypatch.setattr(module, "_fernet", Fernet(Fernet.generate_key()))

    with pytest.raises(module.SecretDecryptError):
        module.decrypt_secret(encrypted)


@pytest.mark.parametrize("garbage", ["enc:v1:中文", "enc:v1:not-base64!!", "enc:v1:"])
def test_corrupted_ciphertext_raises_secret_decrypt_error(garbage):
    """`InvalidToken` 接不住的两类也必须翻成同一个异常。

    `enc:v1:中文` 会在 `.encode("ascii")` 处抛 `UnicodeEncodeError`；不合法的 base64
    抛 `ValueError`。两者原先都会从 ORM 的 `process_result_value` 冒出去，让
    `GET /api/admin/providers` 整体 500——「某一行密钥坏了」不该让后台打不开。
    """
    from app.config import secret_box as module

    with pytest.raises(module.SecretDecryptError):
        module.decrypt_secret(garbage)


def test_null_or_empty_stays_none_or_empty():
    """没配密钥的提供商不能被当成「解密失败」。"""
    from app.config import secret_box as module

    assert module.decrypt_secret(None) == ""
    assert module.decrypt_secret("") == ""

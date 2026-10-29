"""API Key 落库加密与会话摘要（I-06/I-10）。

加密发生在 SQLAlchemy 的 bind/result 边界：**库里是密文、ORM 读出来是明文**。
这个性质在真实数据库上验证，才能覆盖 `TEXT` 列的实际存储形态，以及 `EncryptedText`
与 `Text` 的渲染一致性。
"""

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from app.config import secret_box
from app.db import ModelProvider, async_session_maker


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


async def test_provider_api_key_is_encrypted_in_database():
    """ORM 语义不变（读出来是明文），但库里存的列必须是密文。"""
    async with async_session_maker() as session:
        session.add(
            ModelProvider(
                name="gw",
                slug="gw",
                base_url="http://127.0.0.1:4000/v1",
                api_key="sk-plaintext-key",
                enabled=True,
            )
        )
        await session.commit()
    async with async_session_maker() as session:
        raw = (await session.execute(text("SELECT api_key FROM model_provider"))).scalar_one()
        assert raw.startswith(secret_box.PREFIX)
        assert "sk-plaintext-key" not in raw

        # ORM 读出来必须是明文，业务代码不用感知加密
        loaded = (await session.scalars(select(ModelProvider))).first()
        assert loaded is not None and loaded.api_key == "sk-plaintext-key"


async def test_audit_finds_providers_whose_key_cannot_be_decrypted(monkeypatch):
    """能说出**是哪几个**提供商的密钥解不开——这是「换 AUTH_SECRET」唯一可诊断的出口。

    不解密就分发到下游时，这个信息完全不可得：`EncryptedText` 交出去的是明文或 None，
    「没配」与「解不开」在那一层已经不可区分。
    """
    from app.config import secret_box as module

    good_key = module.encrypt_secret("sk-good")
    # 直接写密文原文，绕过 ORM 的加密（模拟「库里已经是旧密钥的密文」）
    async with async_session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO model_provider (id, name, slug, base_url, api_key, auth_style,"
                " extra_headers, enabled, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :name, :slug, 'http://127.0.0.1:4000/v1', :key,"
                " 'bearer', '{}'::jsonb, true, now(), now())"
            ),
            {"name": "old", "slug": f"old-{uuid.uuid4().hex[:8]}", "key": good_key},
        )
        await session.commit()

    from app.db import audit_provider_key_decryption

    assert await audit_provider_key_decryption() == []

    # 换掉密钥 = 现存的密文全部解不开
    monkeypatch.setattr(module, "_fernet", Fernet(Fernet.generate_key()))
    broken = await audit_provider_key_decryption()
    assert len(broken) == 1


async def test_orm_reads_none_for_undecryptable_key(monkeypatch):
    """单行解不开时 ORM 交 `None`，整个列表仍然可用（不能让它变成 500）。"""
    import uuid as uuid_module

    from app.config import secret_box as module

    async with async_session_maker() as session:
        session.add(
            ModelProvider(
                name="gw2",
                slug=f"gw2-{uuid_module.uuid4().hex[:8]}",
                base_url="http://127.0.0.1:4000/v1",
                api_key="sk-plain",
                enabled=True,
            )
        )
        await session.commit()

    monkeypatch.setattr(module, "_fernet", Fernet(Fernet.generate_key()))

    async with async_session_maker() as session:
        row = (await session.scalars(select(ModelProvider))).first()
    assert row is not None
    assert row.api_key is None

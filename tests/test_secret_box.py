"""API Key 落库加密与会话摘要（I-06/I-10）。

加密发生在 SQLAlchemy 的 bind/result 边界：**库里是密文、ORM 读出来是明文**。
这个性质必须在真实数据库上验证——SQLite 时代它碰巧也成立，但那时测不到
`TEXT` 列在 PG 上的实际存储形态（以及 `EncryptedText` 与 `Text` 的渲染一致性）。
"""

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


def test_wrong_key_returns_empty_instead_of_ciphertext(monkeypatch, caplog):
    from app.config import secret_box as module

    encrypted = module.encrypt_secret("sk-secret")
    monkeypatch.setattr(module, "_fernet", Fernet(Fernet.generate_key()))

    with caplog.at_level("ERROR", logger="app.config.secret_box"):
        assert module.decrypt_secret(encrypted) == ""
    assert "解密失败" in caplog.text


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

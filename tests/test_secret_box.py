"""API Key 落库加密与会话摘要（I-06/I-10）。"""

import os

from cryptography.fernet import Fernet
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import secret_box
from app.db import Base, ModelProvider, secure_sqlite_permissions


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


async def test_provider_api_key_is_encrypted_in_database(tmp_path):
    """ORM 语义不变（读出来是明文），但磁盘上的列必须是密文。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection, _record):  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
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
    async with maker() as session:
        raw = (await session.execute(text("SELECT api_key FROM model_provider"))).scalar_one()
        assert raw.startswith(secret_box.PREFIX)
        assert "sk-plaintext-key" not in raw

        # ORM 读出来必须是明文，业务代码不用感知加密
        loaded = (await session.scalars(select(ModelProvider))).first()
        assert loaded is not None and loaded.api_key == "sk-plaintext-key"

    await engine.dispose()


def test_sqlite_permissions_are_tightened(tmp_path):
    path = tmp_path / "perm.db"
    path.write_text("x")
    os.chmod(path, 0o644)

    assert secure_sqlite_permissions(path) is True
    assert (os.stat(path).st_mode & 0o777) == 0o600

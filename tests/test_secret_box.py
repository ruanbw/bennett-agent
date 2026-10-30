"""API Key 落库加密（I-06/I-10）的纯逻辑侧。

加密发生在 SQLAlchemy 的 bind/result 边界：**库里是密文、ORM 读出来是明文**。
这里只锁加密原语本身（往返、旧密文兼容、上一把密钥、解不开必须显式报错）；bind/result 边界
上的落库形态需要真实数据库，已随「零基础设施」改造移出测试层。

密钥轮换（`MultiFernet` + `enc:v2:<kid>:`）的三条兼容性契约也在这里：

1. 旧 `enc:v1:<token>` 密文在 v2 代码下必须仍能解——否则一次升级就是全部提供商凭据作废；
2. 用「上一把密钥」（`PROVIDER_KEY_SECRET_PREVIOUS`）写出的密文必须仍能解——
   否则轮换窗口内所有对话都会 401；
3. 换掉密钥且没有保留上一把时必须抛 `SecretDecryptError`，不能返回空串。
"""

import pytest

from app.config import secret_box

CURRENT_SECRET = "current-provider-key-secret-0123456789abcdef"
PREVIOUS_SECRET = "previous-provider-key-secret-0123456789abcdef"


def _use_keys(monkeypatch, current: str, previous: str | None = None) -> None:
    """把模块级密钥环换成测试用的两把密钥（生产里由 settings 决定）。"""
    monkeypatch.setattr(secret_box, "_KEY_RING", secret_box._build_key_ring(current, previous))


def _v2_ciphertext(secret: str, plaintext: str) -> str:
    """按 v2 格式手工造一条「由指定密钥加密」的密文（模拟轮换前写进库的行）。"""
    token = secret_box._fernet_for(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"enc:v2:{secret_box._kid_for(secret)}:{token}"


def _v1_ciphertext(secret: str, plaintext: str) -> str:
    """按 v1 格式（不带 key id）造一条旧密文：升级前写进库的形态。"""
    token = secret_box._fernet_for(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"enc:v1:{token}"


def test_secret_roundtrip_hides_plaintext(monkeypatch):
    _use_keys(monkeypatch, CURRENT_SECRET)

    encrypted = secret_box.encrypt_secret("sk-super-secret")

    assert encrypted.startswith(secret_box.PREFIX)
    assert "sk-super-secret" not in encrypted
    assert secret_box.decrypt_secret(encrypted) == "sk-super-secret"


def test_ciphertext_carries_current_key_id(monkeypatch):
    """新密文必须带 kid：没有它，轮换脚本无法回答「哪些行还没搬到新密钥下」。"""
    _use_keys(monkeypatch, CURRENT_SECRET)

    encrypted = secret_box.encrypt_secret("sk-1")

    assert encrypted.startswith(f"enc:v2:{secret_box.current_key_id()}:")
    assert secret_box.key_id_of(encrypted) == secret_box.current_key_id()


def test_legacy_v1_ciphertext_still_decrypts(monkeypatch):
    """升级前写进库的 `enc:v1:<token>` 必须继续可解（不带 key id 也一样）。

    这条断了，升级到 MultiFernet 版本的那一刻全部提供商 API Key 就都解不开。
    """
    _use_keys(monkeypatch, CURRENT_SECRET)

    assert secret_box.decrypt_secret(_v1_ciphertext(CURRENT_SECRET, "sk-legacy")) == "sk-legacy"


def test_previous_key_still_decrypts_during_rotation(monkeypatch):
    """轮换窗口：新密钥已生效，但库里的行还是上一把密钥加密的，必须仍能解。"""
    _use_keys(monkeypatch, CURRENT_SECRET, PREVIOUS_SECRET)

    old_ciphertext = _v2_ciphertext(PREVIOUS_SECRET, "sk-rotating")

    assert secret_box.key_id_of(old_ciphertext) != secret_box.current_key_id()
    assert secret_box.decrypt_secret(old_ciphertext) == "sk-rotating"


def test_reencrypting_moves_the_row_to_the_current_key(monkeypatch):
    """轮换脚本的动作：解出来再加密一次，kid 必须变成当前密钥的（否则脚本会永远认为它待处理）。"""
    _use_keys(monkeypatch, CURRENT_SECRET, PREVIOUS_SECRET)
    old_ciphertext = _v2_ciphertext(PREVIOUS_SECRET, "sk-rotating")

    reencrypted = secret_box.encrypt_secret(secret_box.decrypt_secret(old_ciphertext))

    assert secret_box.key_id_of(reencrypted) == secret_box.current_key_id()
    assert secret_box.decrypt_secret(reencrypted) == "sk-rotating"


def test_legacy_plaintext_is_returned_unchanged():
    """升级前写进去的明文行必须还能用，不能要求停机迁移。"""
    assert secret_box.decrypt_secret("sk-legacy") == "sk-legacy"


def test_wrong_key_raises_instead_of_returning_empty(monkeypatch):
    """解不开时必须抛，不能返回空串。

    返回空串会一路静默：`build_auth_headers` 看到空 key 就不发 Authorization → 上游 401 →
    用户看到「上游认证配置问题」；后台显示「未设置」，管理员只会去重填 key。而根因是密钥换过。
    异常让「这一行不可用」成为显式状态。
    """
    encrypted = secret_box.encrypt_secret("sk-secret")
    _use_keys(monkeypatch, "a-completely-different-secret-0123456789")

    with pytest.raises(secret_box.SecretDecryptError):
        secret_box.decrypt_secret(encrypted)


def test_previous_key_is_dropped_when_not_configured(monkeypatch):
    """删掉 `PROVIDER_KEY_SECRET_PREVIOUS` 后旧行就该明确报错——不能假装还能解。"""
    old_ciphertext = _v2_ciphertext(PREVIOUS_SECRET, "sk-old")
    _use_keys(monkeypatch, CURRENT_SECRET)

    with pytest.raises(secret_box.SecretDecryptError):
        secret_box.decrypt_secret(old_ciphertext)


@pytest.mark.parametrize(
    "garbage",
    [
        # v1：`InvalidToken` 接不住的两类也必须翻成同一个异常
        "enc:v1:中文",
        "enc:v1:not-base64!!",
        "enc:v1:",
        # v2：缺 token、缺 kid、非 ASCII
        "enc:v2:abcd:中文",
        "enc:v2:",
        "enc:v2:abcd:",
    ],
)
def test_corrupted_ciphertext_raises_secret_decrypt_error(monkeypatch, garbage):
    """`enc:v1:中文` 会在 `.encode("ascii")` 处抛 `UnicodeEncodeError`；不合法的 base64
    抛 `ValueError`。两者原先都会从 ORM 的 `process_result_value` 冒出去，让
    `GET /api/admin/providers` 整体 500——「某一行密钥坏了」不该让后台打不开。
    """
    _use_keys(monkeypatch, CURRENT_SECRET)

    with pytest.raises(secret_box.SecretDecryptError):
        secret_box.decrypt_secret(garbage)


def test_prefix_marker_matches_both_versions():
    """`PREFIX` 是「密文 or 明文」的判据（`audit_provider_key_decryption` 用它筛行）。

    它必须同时匹配 v1 与 v2，否则升级后审计会把还没重加密的旧行当成明文行跳过——
    正是最该被点名的那批行。
    """
    assert secret_box.PREFIX == "enc:v"
    assert _v1_ciphertext(CURRENT_SECRET, "sk-1").startswith(secret_box.PREFIX)
    assert _v2_ciphertext(CURRENT_SECRET, "sk-1").startswith(secret_box.PREFIX)
    assert not "sk-plaintext".startswith(secret_box.PREFIX)


@pytest.mark.parametrize("value", [None, "", "sk-plaintext", "enc:v1:abc", "enc:v2:", "enc:v2:abcd"])
def test_key_id_of_returns_none_for_rows_without_a_key_id(value):
    """没有 kid 的行（明文 / v1 / 坏格式）不能返回一个假 kid，否则轮换脚本会误判为「已就绪」。"""
    assert secret_box.key_id_of(value) is None


def test_null_or_empty_stays_none_or_empty():
    """没配密钥的提供商不能被当成「解密失败」。"""
    assert secret_box.decrypt_secret(None) == ""
    assert secret_box.decrypt_secret("") == ""

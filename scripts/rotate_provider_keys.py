"""把已加密的提供商 API Key 重加密到当前密钥。

## 为什么需要这一步

`model_provider.api_key` 是 `EncryptedText`，库里存的是 `enc:v2:<kid>:<token>`。轮换加密密钥时，
旧密文只有「上一把密钥」还配在 `PROVIDER_KEY_SECRET_PREVIOUS` 时才解得开；一旦删掉那项配置，
所有没重加密的行就永久不可用（后台显示「未配置密钥」，每次对话 401）。本脚本是轮换流程的后半段：
把行逐条从上一把密钥搬到当前密钥下，搬完才能安全地删掉上一把。

## 用法

    # 1) PROVIDER_KEY_SECRET=新密钥、PROVIDER_KEY_SECRET_PREVIOUS=旧密钥，重启应用
    # 2) 试运行（默认，不写库）：只报告会改哪些行
    uv run python scripts/rotate_provider_keys.py
    # 3) 真正写库
    uv run python scripts/rotate_provider_keys.py --apply
    # 4) 输出里不再有「解不开」的行后，删掉 PROVIDER_KEY_SECRET_PREVIOUS

退出码：0 全部就绪（含试运行且无待办）；1 有行解不开或写库失败。

必须走原始 SQL：经过 `EncryptedText` 后读到的是明文，分不清「没配密钥」与「已经是当前密钥」
（见 `app/db/maintenance.audit_provider_key_decryption` 的同一理由）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# 允许 `python scripts/rotate_provider_keys.py` 直接跑（否则拿不到仓库根的 app 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import secret_box, settings  # noqa: E402
from app.config.secret_box import SecretDecryptError  # noqa: E402
from app.db import async_session_maker  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass(frozen=True, slots=True)
class _Pending:
    """一行待重加密的凭据；`ciphertext` 已经是当前密钥下的密文。"""

    provider_id: uuid.UUID
    name: str
    ciphertext: str


async def _collect() -> tuple[list[_Pending], list[tuple[str, str]], int]:
    """读全表并算出待重加密的行，返回 `(待改, 解不开的 (id, name), 已是当前密钥的行数)`。"""
    async with async_session_maker() as session:
        rows = (await session.execute(text("SELECT id, name, api_key FROM model_provider ORDER BY name"))).all()

    current_kid = secret_box.current_key_id()
    pending: list[_Pending] = []
    broken: list[tuple[str, str]] = []
    up_to_date = 0
    for provider_id, name, raw in rows:
        if not raw:
            continue  # 没配密钥的行：没什么可加密的
        if not str(raw).startswith(secret_box.PREFIX):
            continue  # 升级前写入的明文行：下次在后台保存时会自然加密
        if secret_box.key_id_of(str(raw)) == current_kid:
            up_to_date += 1
            continue
        try:
            plaintext = secret_box.decrypt_secret(str(raw))
        except SecretDecryptError:
            # 解不开说明上一把密钥没配（或被删了）：这一行只能人工重填，脚本不能猜。
            broken.append((str(provider_id), str(name)))
            continue
        reencrypted = secret_box.encrypt_secret(plaintext)
        # 写库前先自证可读：写进一条解不开的密文比不轮换更糟——凭据会彻底丢失。
        if secret_box.decrypt_secret(reencrypted) != plaintext:
            broken.append((str(provider_id), str(name)))
            continue
        pending.append(_Pending(provider_id=provider_id, name=str(name), ciphertext=reencrypted))
    return pending, broken, up_to_date


async def _apply(pending: list[_Pending]) -> None:
    """一次事务写完全部行：失败就整体回滚，不留「一半新密钥、一半旧密钥」的中间态。"""
    async with async_session_maker() as session:
        for item in pending:
            await session.execute(
                text("UPDATE model_provider SET api_key = :api_key WHERE id = :provider_id"),
                {"api_key": item.ciphertext, "provider_id": item.provider_id},
            )
        await session.commit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把已加密的提供商 API Key 重加密到当前密钥",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库（默认只试运行，不修改任何行）",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    if not (settings.provider_key_secret or "").strip():
        # 没有独立密钥时「当前密钥」就是 AUTH_SECRET，轮换它等于同时作废签名令牌，先说清楚。
        print("提示：PROVIDER_KEY_SECRET 未设置，当前加密密钥回退为 AUTH_SECRET。")
    if not (settings.provider_key_secret_previous or "").strip():
        print("提示：PROVIDER_KEY_SECRET_PREVIOUS 未设置，旧密钥写出的密文将全部解不开。")

    pending, broken, up_to_date = await _collect()

    print(
        f"当前密钥 kid={secret_box.current_key_id()}："
        f"已是当前密钥 {up_to_date} 行，待重加密 {len(pending)} 行，解不开 {len(broken)} 行。"
    )
    for item in pending:
        print(f"  - {item.name} ({item.provider_id})")
    for provider_id, name in broken:
        print(
            f"  ! {name} ({provider_id}) 解不开：请确认 PROVIDER_KEY_SECRET_PREVIOUS 是上一把密钥，"
            "或在管理后台重新填写该提供商的 API Key",
            file=sys.stderr,
        )

    if not args.apply:
        print("试运行结束，未写库。确认无误后加 --apply。")
        return EXIT_OK if not broken else EXIT_FAILED

    if pending:
        try:
            await _apply(pending)
        except Exception as exc:  # noqa: BLE001 - 脚本的出口，错误信息要原样给运维看
            print(f"错误：写库失败，已回滚，没有行被修改（{type(exc).__name__}: {exc}）", file=sys.stderr)
            return EXIT_FAILED
        print(f"✓ 已重加密 {len(pending)} 行。")
    else:
        print("没有需要重加密的行。")

    if broken:
        print("仍有解不开的行，见上文；处理完之前不要删 PROVIDER_KEY_SECRET_PREVIOUS。", file=sys.stderr)
        return EXIT_FAILED
    print("全部行都已使用当前密钥，可以删除 PROVIDER_KEY_SECRET_PREVIOUS。")
    return EXIT_OK


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

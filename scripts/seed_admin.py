"""播种初始超级管理员（幂等）。

## 为什么是这个形态

管理员是**部署者已知的数据**，不是靠公开接口抢注出来的，所以用一次性写入：导库即可登录。
邮箱在这里仅作账号标识：项目没有邮箱验证，因此**不能**把「邮箱属于某人」当作授权依据。

## 用法

    uv run python scripts/seed_admin.py --email admin@example.com --password 'S3cret-pass'
    uv run python scripts/seed_admin.py --email admin@example.com            # 交互输入密码
    uv run python scripts/seed_admin.py --email admin@example.com --force-reset

也可以从环境变量读（避免长驻 shell history）：

    SEED_ADMIN_EMAIL=admin@example.com SEED_ADMIN_PASSWORD='...' \\
        uv run python scripts/seed_admin.py

## 幂等语义

- 账号不存在 → 创建为 `superadmin`；
- 账号已存在且已是 `superadmin` → 不动（除非 `--force-reset`）；
- 账号已存在但是普通用户 / `admin` → **默认只报错不升权**，必须显式 `--promote`。

最后一条是刻意的：安静地把一个已存在的普通账号提升为最高权限，等于给「猜到某个
已注册邮箱」留了一条后门。要升就说清楚。

## 密码与哈希

密码交给 `UserManager` 处理，因此与线上登录用的是同一个 argon2 配置与同一套长度校验
（`MIN_PASSWORD_LENGTH`）。手写 `PasswordHelper()` 也能算哈希，但一旦框架升级换了
算法或参数，脚本生成的哈希就与登录校验脱钩，而这种故障只在真实登录时才暴露。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

# 允许 `python scripts/seed_admin.py` 直接跑（否则拿不到仓库根的 app 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi_users.exceptions import (
    InvalidPasswordException,  # noqa: E402
    UserAlreadyExists,  # noqa: E402
)
from sqlalchemy import select  # noqa: E402

from app.db import User, async_session_maker, verify_schema_at_head  # noqa: E402
from app.roles import ROLE_SUPERADMIN, Role  # noqa: E402
from app.schemas.user import UserCreate, UserUpdate  # noqa: E402
from app.services.auth import MIN_PASSWORD_LENGTH, UserManager, apply_role  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFLICT = 2


class _ScriptedUserManager(UserManager):
    """只用 `create()` / `update()`，不碰请求级依赖。

    `UserManager.__init__` 只要求一个 user_db，而这两个方法的 `request` 参数是可选的
    ——所以这里不需要伪造 FastAPI 的 Request。
    """


def _manager(session) -> _ScriptedUserManager:  # noqa: ANN001 - AsyncSession
    from fastapi_users.db import SQLAlchemyUserDatabase

    return _ScriptedUserManager(SQLAlchemyUserDatabase(session, User))


async def _find_user(email: str) -> User | None:
    async with async_session_maker() as session:
        return await session.scalar(select(User).where(User.email == email))


async def _create_superadmin(email: str, password: str) -> User:
    async with async_session_maker() as session:
        manager = _manager(session)
        # safe=True 会屏蔽 is_superuser 等敏感字段，所以这里传 safe=False，建完再
        # apply_role 把 role 写进去。不能把 role 放进 UserCreate：那个模型刻意不含
        # role（客户端不能指定角色），而 create_update_dict 只认模型上声明过的字段。
        user = await manager.create(UserCreate(email=email, password=password), safe=False)
        apply_role(user, Role.SUPERADMIN)
        await session.commit()
        await session.refresh(user)
        return user


async def _reset_password(user: User, password: str) -> User:
    """改密码，并让该用户的全部旧会话立即失效。

    会话清理不是可选项：密码重置的典型场景就是「密码可能泄露了」。旧 token 还能用
    的话，这次重置等于只防住了新登录。`UserManager.on_after_update` 已经会删该用户
    的全部 access_token 行，这里只需要走 `manager.update()` 就能自动获得那个行为。
    """
    async with async_session_maker() as session:
        manager = _manager(session)
        merged = await session.merge(user)
        updated = await manager.update(UserUpdate(password=password), merged, safe=False)
        await session.commit()
        await session.refresh(updated)
        return updated


async def _promote(user: User) -> User:
    async with async_session_maker() as session:
        merged = await session.merge(user)
        apply_role(merged, Role.SUPERADMIN)
        await session.commit()
        await session.refresh(merged)
        return merged


def _resolve_password(args: argparse.Namespace) -> str | None:
    """命令行 > 环境变量 > 交互输入。返回 None 表示应当中止。"""
    if args.password:
        return args.password

    env_password = os.environ.get("SEED_ADMIN_PASSWORD")
    if env_password:
        return env_password

    if not sys.stdin.isatty():
        print(
            "错误：没有可用的密码。非交互环境请传 --password 或设置 SEED_ADMIN_PASSWORD。",
            file=sys.stderr,
        )
        return None

    first = getpass.getpass("请输入管理员密码：")
    second = getpass.getpass("再输入一次：")
    if first != second:
        print("错误：两次输入的密码不一致。", file=sys.stderr)
        return None
    return first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="播种（或重置）一个超级管理员账号",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("SEED_ADMIN_EMAIL"),
        help="管理员邮箱；也可用环境变量 SEED_ADMIN_EMAIL",
    )
    parser.add_argument(
        "--password",
        help="管理员密码；缺省时依次尝试 SEED_ADMIN_PASSWORD、交互输入",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="账号已存在但角色低于 superadmin 时，提升为 superadmin",
    )
    parser.add_argument(
        "--force-reset",
        action="store_true",
        help="账号已是 superadmin 时也重置密码（密码丢失时的恢复通道）",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    email = (args.email or "").strip().lower()
    if not email or "@" not in email:
        print("错误：请用 --email 指定一个合法邮箱（或设置 SEED_ADMIN_EMAIL）。", file=sys.stderr)
        return EXIT_FAILED

    # 建表：执行 sql/init.sql（幂等）。
    # 这里只校验「库结构已到 head」——拿着旧结构写管理员账号的后果是登录时才报
    # UndefinedColumn，而那时运维已经在排查「为什么密码不对」了。
    await verify_schema_at_head()

    existing = await _find_user(email)

    if existing is None:
        password = _resolve_password(args)
        if password is None:
            return EXIT_FAILED
        try:
            created = await _create_superadmin(email, password)
        except InvalidPasswordException as exc:
            print(f"错误：密码不合规（{exc.reason}）。", file=sys.stderr)
            return EXIT_FAILED
        except UserAlreadyExists:
            # 并发跑两次的竞态：另一个进程刚建好。交给下面的分支重新处理。
            print("提示：账号刚被其它进程创建，重新检查……")
            existing = await _find_user(email)
            if existing is None:
                print("错误：建号失败且账号不存在，请重试。", file=sys.stderr)
                return EXIT_FAILED
        else:
            print(f"✓ 管理员已创建: {created.email}  role={created.role}")
            print("  现在可以用这个邮箱 + 密码登录管理后台。")
            return EXIT_OK

    # --- 账号已存在 ---
    current = existing.role_level

    if current < Role.SUPERADMIN:
        if not args.promote:
            print(
                f"错误：{existing.email} 已存在，当前角色是 {existing.role}。\n"
                f"      若确实要把它提为 {ROLE_SUPERADMIN}，请加 --promote。",
                file=sys.stderr,
            )
            return EXIT_CONFLICT
        promoted = await _promote(existing)
        print(f"✓ 已提升为 {ROLE_SUPERADMIN}: {promoted.email}")
        existing = promoted
        if not args.force_reset:
            return EXIT_OK

    if not args.force_reset:
        print(f"= {existing.email} 已是 {ROLE_SUPERADMIN}，未做任何修改。")
        print("  需要重置密码请加 --force-reset。")
        return EXIT_OK

    password = _resolve_password(args)
    if password is None:
        return EXIT_FAILED
    try:
        updated = await _reset_password(existing, password)
    except InvalidPasswordException as exc:
        print(f"错误：密码不合规（{exc.reason}，至少 {MIN_PASSWORD_LENGTH} 位）。", file=sys.stderr)
        return EXIT_FAILED

    print(f"✓ 密码已重置: {updated.email}")
    print("  该账号的旧登录会话已全部失效，需要用新密码重新登录。")
    return EXIT_OK


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

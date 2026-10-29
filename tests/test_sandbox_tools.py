"""沙箱工具的行为锁：越权、截断、审计、重建。

这一层是模型可以直接调用的外部边界，所以验收点不是「功能跑通」，而是四件在别处无法验证的事：

1. **越权在构造上不可达**——工具签名里没有 id；没有作用域时连 provider 一次都不碰。
   这条一旦退化，后面所有检查都只是在兜底。
2. **失败文案不给存在性 oracle**——`SandboxNotFound` 的文案与「不属于你」必须无法区分，
   否则模型可以拿响应差异探测别人有没有环境。
3. **截断是成本控制点**——沙箱的花费只通过「exec 输出进上下文」这一条通路变成 token，
   所以头尾保留与省略字符数标注都要钉住。
4. **重置之后是新机器**——旧机器必须真的被 destroy，而不是换个 ref 继续用。

provider 用 `FakeProvider`：上面四条与 Docker/KVM 无关，用真后端跑反而会把「工具层的
契约」和「后端的实现」搅在一起。数据库是 conftest 的真实 Postgres——审计行与租约状态
是工具层的产出，mock 掉它等于不验。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db import SandboxCommand, SandboxLease, User, async_session_maker
from app.services import sandbox as sandbox_service
from app.services.sandbox.base import (
    MAX_EXEC_TIMEOUT_S,
    MAX_OUTPUT_HEAD_CHARS,
    MAX_OUTPUT_TAIL_CHARS,
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxNotFound,
    SandboxProvider,
    SandboxSpec,
    owner_tag,
)
from app.tools.sandbox import (
    MAX_READ_BYTES,
    MAX_WRITE_CHARS,
    render_exec_result,
    sandbox_exec,
    sandbox_read_file,
    sandbox_reset,
    sandbox_write_file,
)
from app.tools.sandbox_scope import sandbox_scope

WORKDIR = settings.sandbox_workdir.rstrip("/")


class FakeProvider:
    """只记录调用，不碰任何真实后端。

    契约只有 6 个方法，所以假件也很小——这本身就是「契约做小」的收益：能用二十行模拟出
    一个完整后端，才可能给授权与生命周期写穷举测试。

    被调用的顺序也一并记下来（`calls`），因为「重置后先 destroy 再 create」是一个**顺序**
    断言，只看最终状态看不出来。
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created: list[SandboxSpec] = []
        self.execs: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.destroyed: list[dict[str, Any]] = []
        self.exec_result = ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)
        self.exec_error: Exception | None = None
        self.destroy_error: Exception | None = None
        self.file_content = b""
        self._seq = 0

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.calls.append("create")
        self.created.append(spec)
        self._seq += 1
        return SandboxHandle(ref=f"fake-{self._seq}", ready_ms=5)

    async def exec(
        self,
        ref: str,
        command: str,
        *,
        timeout_s: int = 120,
        workdir: str | None = None,
        owner_tag: str | None = None,
    ) -> ExecResult:
        self.calls.append("exec")
        self.execs.append(
            {
                "ref": ref,
                "command": command,
                "timeout_s": timeout_s,
                "workdir": workdir,
                "owner_tag": owner_tag,
            }
        )
        if self.exec_error is not None:
            raise self.exec_error
        return self.exec_result

    async def put_file(self, ref: str, path: str, data: bytes, *, owner_tag: str | None = None) -> None:
        self.calls.append("put_file")
        self.writes.append({"ref": ref, "path": path, "data": data, "owner_tag": owner_tag})

    async def get_file(self, ref: str, path: str, *, max_bytes: int, owner_tag: str | None = None) -> bytes:
        self.calls.append("get_file")
        self.reads.append({"ref": ref, "path": path, "max_bytes": max_bytes, "owner_tag": owner_tag})
        return self.file_content

    async def destroy(self, ref: str, *, owner_tag: str | None = None) -> None:
        self.calls.append("destroy")
        self.destroyed.append({"ref": ref, "owner_tag": owner_tag})
        if self.destroy_error is not None:
            raise self.destroy_error

    async def is_alive(self, ref: str) -> bool:
        self.calls.append("is_alive")
        return True


@dataclass
class Env:
    provider: FakeProvider
    user_id: uuid.UUID
    session_id: str


@pytest.fixture
async def env() -> Env:
    provider = FakeProvider()
    # runtime_checkable 的 Protocol：假件漏实现一个方法时这里立刻失败，而不是等到某个
    # 分支第一次被走到。
    assert isinstance(provider, SandboxProvider)
    sandbox_service.set_provider_for_tests(provider)

    user_id = uuid.uuid4()
    async with async_session_maker() as session:
        session.add(
            User(
                id=user_id,
                email=f"sbx-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        await session.commit()

    try:
        yield Env(provider=provider, user_id=user_id, session_id=f"sess_{uuid.uuid4().hex}")
    finally:
        sandbox_service.set_provider_for_tests(None)


async def _count(model: Any) -> int:  # noqa: ANN401 - SQLAlchemy 模型类
    async with async_session_maker() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def _lease_rows() -> list[SandboxLease]:
    async with async_session_maker() as session:
        return list(await session.scalars(select(SandboxLease).order_by(SandboxLease.created_at, SandboxLease.id)))


# ---------------------------------------------------------------------
# ① 没有作用域：四个工具都不碰 provider
# ---------------------------------------------------------------------
async def test_all_tools_refuse_without_scope(env: Env):
    """未登录没有身份：明确拒绝，且**一次都不碰** provider。

    「不碰」比「回一句拒绝」重要一个量级：任何在检查作用域之前就解析租约的实现，
    都会在最常见的匿名路径上建出一台不属于任何人的机器。
    """
    path = f"{WORKDIR}/a.txt"
    results = [
        await sandbox_exec(None, "ls"),
        await sandbox_write_file(None, path, "x"),
        await sandbox_read_file(None, path),
        await sandbox_reset(None),
    ]

    for text in results:
        assert "没有关联用户" in text
        assert "执行环境" in text

    assert env.provider.calls == []
    assert await _count(SandboxLease) == 0


# ---------------------------------------------------------------------
# ② 大输出截断：保留头尾 + 标注省略字符数
# ---------------------------------------------------------------------
async def test_large_output_keeps_head_and_tail(env: Env):
    head = "HEAD-MARK" + "a" * 5_000
    middle = "MIDDLE-MARK"
    tail = "b" * 3_000 + "TAIL-MARK"
    stdout = head + middle + tail
    env.provider.exec_result = ExecResult(exit_code=0, stdout=stdout, stderr="", duration_ms=12)

    with sandbox_scope(env.user_id, env.session_id):
        text = await sandbox_exec(None, "seq 1 100000")

    omitted = len(stdout) - MAX_OUTPUT_HEAD_CHARS - MAX_OUTPUT_TAIL_CHARS
    assert "HEAD-MARK" in text
    assert "TAIL-MARK" in text
    assert "MIDDLE-MARK" not in text
    assert "中间省略" in text
    assert f"{omitted:,}" in text
    assert len(text) < len(stdout)
    assert "输出已截断" in text


async def test_render_marks_stderr_truncation_shorter_than_stdout():
    """stderr 的阈值比 stdout 短：几万行重复报错对模型没有新信息。"""
    result = ExecResult(exit_code=1, stdout="", stderr="E" * 10_000, duration_ms=3)
    text = render_exec_result(result, "cat missing")

    assert "中间省略 7,000 个字符" in text
    assert "退出码：1" in text
    assert "标准输出" in text and "（无输出）" in text


async def test_render_timeout_says_how_to_fix_it():
    """超时不能说成「环境坏了」：那会让模型去重建沙箱，而重来一遍还是同一条命令。"""
    result = ExecResult(exit_code=None, stdout="partial output", stderr="", duration_ms=900_000, timed_out=True)
    text = render_exec_result(result, "python3 slow.py")

    assert "命令超时" in text
    assert "timeout_s" in text
    assert "partial output" in text
    assert "900.0s" in text
    assert "不可用" not in text
    assert "重建" not in text


async def test_render_does_not_truncate_small_output():
    result = ExecResult(exit_code=0, stdout="ok\n", stderr="", duration_ms=5)
    text = render_exec_result(result, "true")

    assert "ok" in text
    assert "中间省略" not in text
    assert "输出已截断" not in text


# ---------------------------------------------------------------------
# ③ SandboxNotFound 的文案不能泄露「这可能是别人的」
# ---------------------------------------------------------------------
async def test_not_found_message_has_no_ownership_wording(env: Env):
    env.provider.exec_error = SandboxNotFound("container fake-1 not found (owner=deadbeef)")

    with sandbox_scope(env.user_id, env.session_id):
        text = await sandbox_exec(None, "ls")

    assert "执行环境不存在或已回收" in text
    for leaked in ("不属于", "无权", "越权", "权限", "forbidden", "permission", "fake-1", "deadbeef"):
        assert leaked not in text


# ---------------------------------------------------------------------
# ④ exec 写一条 sandbox_command 审计
# ---------------------------------------------------------------------
async def test_exec_records_one_command_audit_row(env: Env):
    env.provider.exec_result = ExecResult(exit_code=1, stdout="line-1\n", stderr="boom\n", duration_ms=7)

    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "cat missing.txt")

    leases = await _lease_rows()
    assert len(leases) == 1
    assert leases[0].state == "ready"
    assert leases[0].provider_ref == env.provider.execs[0]["ref"]

    async with async_session_maker() as session:
        rows = list(await session.scalars(select(SandboxCommand).where(SandboxCommand.lease_id == leases[0].id)))

    assert len(rows) == 1
    row = rows[0]
    assert row.seq == 1
    assert row.command == "cat missing.txt"
    assert row.exit_code == 1
    assert row.duration_ms == 7
    assert row.output_head is not None and "line-1" in row.output_head
    assert row.truncated is False


async def test_exec_marks_audit_row_truncated_for_big_output(env: Env):
    env.provider.exec_result = ExecResult(exit_code=0, stdout="x" * 50_000, stderr="", duration_ms=4)

    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "cat big.json")

    async with async_session_maker() as session:
        row = (await session.scalars(select(SandboxCommand))).one()

    assert row.truncated is True
    # 审计存的是「模型当时看到了什么」：头 + 尾 + 「省略 N 个字符」标记，不是 50k 原始输出。
    # 上限 = 两段之和 + 标记本身的版面开销；只存头会丢掉报错的最后一行，
    # 而那恰好是最需要回看的部分。
    assert len(row.output_head or "") <= MAX_OUTPUT_HEAD_CHARS + MAX_OUTPUT_TAIL_CHARS + 200
    assert "省略" in (row.output_head or "")
    assert (row.output_head or "").count("x") <= MAX_OUTPUT_HEAD_CHARS + MAX_OUTPUT_TAIL_CHARS


# ---------------------------------------------------------------------
# ⑤ reset 之后是新机器
# ---------------------------------------------------------------------
async def test_reset_destroys_machine_and_next_exec_creates_new_one(env: Env):
    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "ls")
        first_ref = env.provider.execs[-1]["ref"]

        reset_text = await sandbox_reset(None)
        assert "已重置" in reset_text

        await sandbox_exec(None, "ls")
        second_ref = env.provider.execs[-1]["ref"]

    assert first_ref != second_ref
    assert len(env.provider.created) == 2
    assert env.provider.destroyed == [{"ref": first_ref, "owner_tag": owner_tag(env.user_id)}]
    # 顺序：先销毁旧的，再在下一轮建新的。
    # 一次重置 = 销毁旧机器；新机器是在下一次执行时按需建的，不是重置时顺便开的。
    assert env.provider.calls == ["create", "exec", "destroy", "create", "exec"]

    leases = await _lease_rows()
    assert [lease.state for lease in leases] == ["destroyed", "ready"]
    assert leases[0].destroy_reason == "user_reset"


async def test_reset_marks_destroy_failed_when_provider_fails(env: Env):
    """销毁失败不能停在 destroying：那个状态既不会被回收重试，也不再是活动租约。"""
    env.provider.destroy_error = SandboxError("docker daemon: cannot kill container abc123")

    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "ls")
        text = await sandbox_reset(None)

    assert "执行环境暂时不可用" in text
    assert "abc123" not in text
    leases = await _lease_rows()
    assert leases[0].state == "destroy_failed"
    assert leases[0].destroy_reason == "user_reset"


async def test_reset_without_environment_does_not_create_one(env: Env):
    """重置不该顺手建一台机器：用户可能只是想说「清掉」，然后再也不用。"""
    with sandbox_scope(env.user_id, env.session_id):
        text = await sandbox_reset(None)

    assert "无需重置" in text
    assert env.provider.created == []


# ---------------------------------------------------------------------
# 授权：租约按 (user_id, session_id) 解析，模型侧没有任何句柄
# ---------------------------------------------------------------------
async def test_provider_always_receives_owner_tag(env: Env):
    """归属标记是第二道证人：每次调用都必须带上它。"""
    env.provider.file_content = b"hello"
    path = f"{WORKDIR}/note.txt"

    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "ls")
        await sandbox_write_file(None, path, "hi")
        await sandbox_read_file(None, path)

    expected = owner_tag(env.user_id)
    assert env.provider.created[0].owner_tag == expected
    assert env.provider.execs[0]["owner_tag"] == expected
    assert env.provider.writes[0]["owner_tag"] == expected
    assert env.provider.reads[0]["owner_tag"] == expected


async def test_two_sessions_of_same_user_share_one_machine(env: Env):
    """同一个用户的两个会话**共用一台机器**。

    这是刻意的产品决定：会话 1 装好的依赖、写到一半的结果，切到会话 2 接着用。
    代价（写进断言里，免得以后被当 bug 修）是同一个 workspace——同一用户的不同会话会
    看到彼此的文件，它们属于同一个人。

    复用时会改写 `session_id`：它现在只是「最后是哪个会话在用」的归因列。
    """
    other_session = f"sess_{uuid.uuid4().hex}"

    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "ls")
    with sandbox_scope(env.user_id, other_session):
        await sandbox_exec(None, "ls")

    assert len(env.provider.created) == 1, "每用户一台：第二个会话不该再建机"
    assert env.provider.execs[0]["ref"] == env.provider.execs[1]["ref"]
    assert await _count(SandboxLease) == 1
    async with async_session_maker() as session:
        rows = (await session.scalars(select(SandboxLease))).all()
    assert rows[0].session_id == other_session


async def test_same_session_reuses_machine(env: Env):
    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "ls")
        await sandbox_exec(None, "pwd")

    assert len(env.provider.created) == 1
    assert env.provider.execs[0]["ref"] == env.provider.execs[1]["ref"]


async def test_scope_without_session_id_is_no_scope(env: Env):
    """没有会话就没有机器可寻址：不许退化成「该用户的默认机器」。"""
    with sandbox_scope(env.user_id, ""):
        text = await sandbox_exec(None, "ls")

    assert "没有关联用户" in text
    assert env.provider.calls == []


# ---------------------------------------------------------------------
# 输入校验：路径、写入上限、超时夹紧
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "etc/passwd",
        "/etc/passwd",
        f"{WORKDIR}/../etc/passwd",
        f"{WORKDIR}/a/../../b",
        "",
    ],
)
async def test_paths_outside_workspace_are_rejected(env: Env, path: str):
    with sandbox_scope(env.user_id, env.session_id):
        write_text = await sandbox_write_file(None, path, "x")
        read_text = await sandbox_read_file(None, path)

    for text in (write_text, read_text):
        assert "路径不可用" in text
        assert WORKDIR in text
    assert env.provider.calls == []
    assert await _count(SandboxLease) == 0


async def test_write_rejects_overlong_content_instead_of_truncating(env: Env):
    """静默截断会写下半截脚本，报错发生在下一次执行时——那时已经很难归因。"""
    path = f"{WORKDIR}/big.py"

    with sandbox_scope(env.user_id, env.session_id):
        text = await sandbox_write_file(None, path, "x" * (MAX_WRITE_CHARS + 1))

    assert "超过单次写入上限" in text
    assert env.provider.writes == []
    assert env.provider.created == []


async def test_read_clamps_max_bytes(env: Env):
    path = f"{WORKDIR}/a.txt"
    env.provider.file_content = b"payload"

    with sandbox_scope(env.user_id, env.session_id):
        text = await sandbox_read_file(None, path, max_bytes=10_000_000)

    assert "payload" in text
    assert env.provider.reads[0]["max_bytes"] == MAX_READ_BYTES


async def test_exec_clamps_timeout_and_passes_command(env: Env):
    with sandbox_scope(env.user_id, env.session_id):
        await sandbox_exec(None, "  python3 a.py  ", timeout_s=100_000)

    call = env.provider.execs[0]
    assert call["command"] == "python3 a.py"
    assert call["timeout_s"] == MAX_EXEC_TIMEOUT_S
    assert call["workdir"] == settings.sandbox_workdir

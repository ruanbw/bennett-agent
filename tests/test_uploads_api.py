"""上传的端到端行为：HTTP → 对象存储 → worker → 知识库。

这一层是**集成测试**，需要真实 Postgres / Redis / 对象存储（见 tests/conftest.py）。
刻意不 mock 存储：文件上传的 bug 大多发生在「本地都好好的，一换成真实对象存储就
403 / 键写错 / 桶不存在」，而 mock 掉的正是这段。

embedding 用确定性替身（不联网、不烧配额），其余全部真跑：

    POST /api/uploads ──► RustFS（原件）──► worker（解析 + 切分）──► Postgres（片段）

断言的重点是**状态机与副作用**，不是 HTTP 形状：

1. 上传后是 `pending`，原件真的在对象存储里；
2. worker 处理后变 `ready`，并产生一篇可检索的知识库文档；
3. 重复上传同一内容去重（不产生第二个原件、不写第二篇文档）；
4. 删除上传会同时删掉原件与派生文档（否则「删了但检索还在」）；
5. 解析失败是**不可重试**的终态，且失败原因写给用户看。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.api.uploads import router as uploads_router
from app.config import settings
from app.db import (
    UPLOAD_STATUS_PENDING,
    UPLOAD_STATUS_READY,
    KnowledgeDocument,
    UploadedFile,
    User,
    async_session_maker,
)
from app.services.auth import current_active_user
from app.services.rag import store as knowledge_store
from app.services.rag.embeddings import EmbeddingResult
from app.services.storage import get_object_store
from app.services.uploads import parsers
from app.services.uploads.worker import process_pending_once

#: 假向量的维度。与真实模型无关，只要同一批内一致即可（入库层会校验一致性）。
FAKE_DIMENSIONS = 8


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    """把 embedding 换成确定性实现：这些用例验的是管线，不是上游 API。"""
    monkeypatch.setattr(settings, "embedding_model", "fake-embedding")
    monkeypatch.setattr(settings, "uploads_enabled", True)

    async def fake_embed(texts, *, target=None, client=None):  # noqa: ANN001, ARG001
        vectors = [[1.0] + [0.0] * (FAKE_DIMENSIONS - 1) for _ in texts]
        return EmbeddingResult(vectors=vectors, model="fake-embedding", dimensions=FAKE_DIMENSIONS)

    monkeypatch.setattr(knowledge_store, "embed_texts", fake_embed)


@pytest.fixture
async def app_user() -> User:
    """真实用户行：`uploaded_file.user_id` 有外键，假对象插不进去。"""
    user = User(
        id=uuid.uuid4(),
        email=f"upload-{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        role="user",
    )
    async with async_session_maker() as session:
        session.add(user)
        await session.commit()
    return user


@pytest.fixture
def client(app_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(uploads_router)
    # 认证在 HTTP 层已被单独覆盖（tests/test_auth_session.py），这里只关心上传本身
    app.dependency_overrides[current_active_user] = lambda: app_user
    return TestClient(app)


def _upload(client: TestClient, name: str, content: bytes, mime: str = "text/markdown"):
    return client.post("/api/uploads", files={"file": (name, content, mime)})


def test_upload_then_worker_makes_it_searchable(client: TestClient):
    response = _upload(client, "退款政策.md", "# 退款政策\n\n七天无理由退货。".encode())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == UPLOAD_STATUS_PENDING
    assert body["duplicate"] is False

    # 原件必须真的落在对象存储里（而不是只写了一条数据库记录）
    async def _exists() -> bool:
        async with async_session_maker() as session:
            record = await session.get(UploadedFile, uuid.UUID(body["id"]))
            assert record is not None
            return await get_object_store().exists(record.storage_key)

    assert asyncio.run(_exists()) is True

    assert asyncio.run(process_pending_once()) is True

    listing = client.get("/api/uploads").json()
    item = listing["items"][0]
    assert item["status"] == UPLOAD_STATUS_READY
    assert item["document_id"] is not None
    assert listing["used_bytes"] == item["size_bytes"]

    async def _document() -> KnowledgeDocument:
        async with async_session_maker() as session:
            document = await session.get(KnowledgeDocument, uuid.UUID(item["document_id"]))
            assert document is not None
            return document

    document = asyncio.run(_document())
    assert document.chunk_count >= 1
    assert document.dimensions == FAKE_DIMENSIONS


def test_duplicate_upload_reuses_the_same_record(client: TestClient):
    content = "发票抬头必须是公司全称。".encode()

    first = _upload(client, "发票.md", content).json()
    second = _upload(client, "发票.md", content).json()

    assert second["duplicate"] is True
    assert second["id"] == first["id"]

    listing = client.get("/api/uploads").json()
    assert listing["total"] == 1


def test_oversized_upload_is_rejected_before_storage(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "upload_max_bytes", 16)
    # 中间件是独立的（整个 HTTP 层），这里只验证流式计数这一道
    monkeypatch.setattr(settings, "max_request_body_bytes", 1024 * 1024)

    response = _upload(client, "big.md", b"x" * 64)

    assert response.status_code == 400
    assert "上限" in response.json()["detail"]


def test_unsupported_extension_is_rejected(client: TestClient):
    response = _upload(client, "payload.exe", b"MZ\x90\x00", "application/octet-stream")

    assert response.status_code == 400
    assert ".exe" in response.json()["detail"]


def test_delete_removes_original_and_derived_document(client: TestClient):
    created = _upload(client, "临时.md", "临时内容，用于验证删除。".encode()).json()
    assert asyncio.run(process_pending_once()) is True

    document_id = uuid.UUID(created["id"])
    record_state: dict[str, object] = {}

    async def _capture() -> None:
        async with async_session_maker() as session:
            record = await session.get(UploadedFile, document_id)
            assert record is not None
            record_state["key"] = record.storage_key
            record_state["document_id"] = record.document_id

    asyncio.run(_capture())

    assert client.delete(f"/api/uploads/{created['id']}").status_code == 204

    async def _gone() -> tuple[bool, bool]:
        async with async_session_maker() as session:
            record = await session.get(UploadedFile, document_id)
            document = await session.get(KnowledgeDocument, record_state["document_id"])
            object_exists = await get_object_store().exists(str(record_state["key"]))
            return record is None, (document is None and not object_exists)

    record_gone, derived_gone = asyncio.run(_gone())
    assert record_gone is True
    # 只删记录不删文档是最容易漏的一步：文件列表里没有了，检索却还能查到内容
    assert derived_gone is True


def test_parse_failure_is_terminal_and_explained(client: TestClient, monkeypatch):
    """解析失败不该无限重试：一份坏文件会永远占着 worker 并反复烧 CPU。"""

    def _boom(kind: str, data: bytes) -> str:  # noqa: ANN001, ARG001
        raise parsers.DocumentParseError("PDF 文件损坏或格式不完整，无法解析。")

    monkeypatch.setattr(parsers, "extract_text", _boom)
    created = _upload(client, "坏文件.md", "内容不重要".encode()).json()

    asyncio.run(process_pending_once())

    listing = client.get("/api/uploads").json()
    item = listing["items"][0]
    assert item["id"] == created["id"]
    assert item["status"] == "failed"
    assert "损坏" in (item["error"] or "")
    assert item["attempts"] == 1  # 没有进入重试


def test_upload_requires_authentication():
    """没有覆盖认证依赖时，上传端点必须 401（而不是匿名可写）。"""
    app = FastAPI()
    app.include_router(uploads_router)
    anonymous = TestClient(app)

    response = anonymous.post("/api/uploads", files={"file": ("a.md", b"x", "text/markdown")})

    assert response.status_code == 401

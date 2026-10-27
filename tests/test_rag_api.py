"""Embedding 客户端与知识库 API 的行为锁。

分两部分：

1. `embed_texts` 的**响应解析**：OpenAI 兼容的 `data[].embedding` 必须按 `index`
   排序后返回。不排序会把 A 文本的向量配给 B 块——这种错位不报任何错，只让检索结果
   莫名其妙地不相关。这部分用假的 httpx 客户端，不联网。
2. 知识库 API 的**权限与降级**：未配置 embedding 时必须明确拒绝（409）而不是静默
   成功；删除必须按 `(id, user_id)` 双条件（越权删除比越权读更糟）。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.knowledge import router as knowledge_router
from app.config import settings
from app.services.rag import embeddings
from app.services.rag.embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingError,
    embed_texts,
    embedding_model,
)


# ---------------------------------------------------------------------
# embed_texts
# ---------------------------------------------------------------------
def _client_returning(payload: object, *, status: int = 200) -> httpx.AsyncClient:
    """构造一个总是返回固定响应的 httpx 客户端。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-test")


class _Target:
    base_url = "http://gateway.test:4000"
    api_key = "sk-test"


async def test_embed_texts_sorts_by_index():
    """响应里的 `data` 按规范带 `index`，但**不保证**数组已排序。

    不排序会把 A 文本的向量配给 B 块，检索结果莫名其妙地不相关，而没有任何报错。
    """
    payload = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    client = _client_returning(payload)

    result = await embed_texts(["first", "second"], target=_Target(), client=client)

    assert result.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert result.dimensions == 2


async def test_embed_texts_batches_large_inputs(monkeypatch):
    """超过批大小要分批送，不是一次全发。"""
    monkeypatch.setattr(embeddings, "EMBEDDING_BATCH_SIZE", 2)
    seen_batches: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen_batches.append(len(body["input"]))
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0]} for i in range(len(body["input"]))]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await embed_texts(["a", "b", "c", "d", "e"], target=_Target(), client=client)

    assert seen_batches == [2, 2, 1]


async def test_embed_texts_empty_input_returns_empty_result():
    result = await embed_texts([])

    assert result.vectors == []
    assert result.dimensions == 0


async def test_embed_texts_raises_without_model(monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", None)

    with pytest.raises(EmbeddingError, match="EMBEDDING_MODEL"):
        await embed_texts(["x"], target=_Target())


async def test_embed_texts_raises_on_upstream_status():
    client = _client_returning({"error": "boom"}, status=502)

    with pytest.raises(EmbeddingError, match="502"):
        await embed_texts(["x"], target=_Target(), client=client)


async def test_embed_texts_rejects_wrong_count():
    """返回条数与输入条数不符：zip 之后会错位，必须先拒绝。"""
    payload = {"data": [{"index": 0, "embedding": [1.0]}]}
    client = _client_returning(payload)

    with pytest.raises(EmbeddingError, match="形状"):
        await embed_texts(["a", "b"], target=_Target(), client=client)


async def test_embed_texts_rejects_non_numeric_embedding():
    payload = {"data": [{"index": 0, "embedding": ["a", "b"]}]}
    client = _client_returning(payload)

    with pytest.raises(EmbeddingError, match="形状"):
        await embed_texts(["x"], target=_Target(), client=client)


async def test_embed_texts_rejects_inconsistent_dimensions(monkeypatch):
    """同一批里维度不一致 = 上游行为异常，入库会是脏数据。"""
    monkeypatch.setattr(embeddings, "EMBEDDING_BATCH_SIZE", 5)
    payload = {
        "data": [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [1.0, 0.0, 0.0]},
        ]
    }
    client = _client_returning(payload)

    with pytest.raises(EmbeddingError, match="维度不一致"):
        await embed_texts(["a", "b"], target=_Target(), client=client)


async def test_blank_text_is_replaced_not_skipped(monkeypatch):
    """空文本替换成空格而不是跳过：跳过会让返回条数与输入条数对不上。"""
    sent: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        sent.append(body["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0]} for i in range(len(body["input"]))]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await embed_texts(["", "real"], target=_Target(), client=client)

    assert sent[0][0] == " "
    assert sent[0][1] == "real"


async def test_long_text_is_truncated(monkeypatch):
    sent: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        sent.append(body["input"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await embed_texts(["x" * (embeddings.MAX_EMBEDDING_INPUT_CHARS + 100)], target=_Target(), client=client)

    assert len(sent[0][0]) == embeddings.MAX_EMBEDDING_INPUT_CHARS


def test_embeddings_url_handles_both_base_forms():
    assert embeddings._build_embeddings_url("http://host:4000") == "http://host:4000/v1/embeddings"
    assert embeddings._build_embeddings_url("http://host:4000/v1") == "http://host:4000/v1/embeddings"
    assert embeddings._build_embeddings_url("http://host:4000/v1/") == "http://host:4000/v1/embeddings"


def test_placeholder_model_is_treated_as_unconfigured(monkeypatch):
    """占位值等于没配：否则入库任务会跑起来再逐条 401。"""
    monkeypatch.setattr(settings, "embedding_model", "your-embedding-model-here")

    assert embedding_model() is None


def test_dimension_mismatch_is_a_distinct_error():
    """它是 `EmbeddingError` 的子类，但可以被单独捕获（要求重建索引）。"""
    assert issubclass(EmbeddingDimensionMismatch, EmbeddingError)


# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
async def _tables():
    """确保测试库里有表（同 `tests/test_rag_store.py`：服务层直接调库）。

    用一次性的临时库：`conftest.py` 只把 `DATABASE_URL` 指向临时文件，建表是
    应用启动才做的。
    """
    # 结构由 conftest 执行 sql/init.sql 建立；用例之间由 TRUNCATE 隔离。
    yield


@pytest.fixture
def app_client():
    """只挂知识库路由的最小应用（不启动 lifespan，不连真网关）。"""
    from app.db import User
    from app.services.auth import current_active_user

    app = FastAPI()
    app.include_router(knowledge_router)

    fake_user = User(
        id=__import__("uuid").uuid4(),
        email="api@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        role="user",
    )
    app.dependency_overrides[current_active_user] = lambda: fake_user
    return TestClient(app), fake_user


def test_knowledge_capabilities_reports_unconfigured(monkeypatch, app_client):
    """未配置 embedding 时给出原因与改法，而不是让上传按钮无声失效。"""
    monkeypatch.setattr(settings, "embedding_model", None)
    client, _ = app_client

    response = client.get("/api/knowledge/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert "EMBEDDING_MODEL" in body["reason"]


def test_upload_is_rejected_when_unconfigured(monkeypatch, app_client):
    """409 而不是静默成功。

    让用户以为文档传上去了、实际上检索永远查不到，比直接说「这个部署没开知识库」
    糟得多。
    """
    monkeypatch.setattr(settings, "embedding_model", None)
    client, _ = app_client

    response = client.post("/api/knowledge/documents", json={"title": "t", "text": "正文"})

    assert response.status_code == 409
    assert "EMBEDDING_MODEL" in response.json()["detail"]


def test_search_is_rejected_when_unconfigured(monkeypatch, app_client):
    monkeypatch.setattr(settings, "embedding_model", None)
    client, _ = app_client

    response = client.post("/api/knowledge/search", json={"query": "退款"})

    assert response.status_code == 409


def test_upload_rejects_empty_text(app_client):
    client, _ = app_client

    response = client.post("/api/knowledge/documents", json={"title": "t", "text": ""})

    # schema 的 min_length 挡在最前面
    assert response.status_code == 422


def test_delete_missing_document_returns_404(app_client):
    client, _ = app_client

    response = client.delete("/api/knowledge/documents/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


def test_delete_with_malformed_id_returns_404_not_500(app_client):
    """坏 id 是调用方的问题，不是服务端错误——返回 404 而不是抛 ValueError。"""
    client, _ = app_client

    response = client.delete("/api/knowledge/documents/not-a-uuid")

    assert response.status_code == 404


# ---------------------------------------------------------------------
# 占位模型名
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["your-embedding-model-here", "replace-me", "placeholder", "my-embedding-model", "  "],
)
def test_placeholder_embedding_models_are_unconfigured(monkeypatch, value):
    monkeypatch.setattr(settings, "embedding_model", value)

    assert embedding_model() is None


@pytest.mark.parametrize("value", ["text-embedding-3-small", "bge-m3", "text-embedding-v3"])
def test_real_embedding_models_are_accepted(monkeypatch, value):
    monkeypatch.setattr(settings, "embedding_model", value)

    assert embedding_model() == value

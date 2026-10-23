"""知识库检索工具的行为锁。

这个工具是模型可调用的**外部边界**，因此两条最重要：

1. `user_id` 绝不来自模型参数——它由服务端通过 ContextVar 注入。模型如果能指定
   user_id，就等于可以遍历整个实例的知识库。
2. 没有作用域（CLI / 未登录）与「检索不到」是**两件事**：前者该说明"
   当前会话没有知识库"，后者该建议换个关键词。文案混了，模型就会把「没配」当成
   「没找到」继续瞎查。
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.services.rag import store
from app.tools import knowledge_search
from app.tools.knowledge_scope import knowledge_scope
from app.tools.knowledge_search import (
    MAX_QUERY_CHARS,
    is_knowledge_base_enabled,
    search_knowledge_base,
)


@pytest.fixture(autouse=True)
async def _tables():
    """建表：个别用例若漏了 mock 会走到真实检索路径，此时不该报「no such table」。"""
    # 结构由 conftest 的 `alembic upgrade head` 建立；用例之间由 TRUNCATE 隔离。
    yield


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "fake-embedding")
    yield


async def test_tool_reports_missing_scope():
    """CLI 没有用户概念：明确说「没有关联知识库」，不要假装能查。"""
    result = await search_knowledge_base(None, "退款")

    assert "没有关联知识库" in result


async def test_tool_reports_empty_query():
    with knowledge_scope(uuid.uuid4()):
        result = await search_knowledge_base(None, "   ")

    assert "关键词为空" in result


async def test_tool_says_not_found_distinctly(monkeypatch):
    """「没找到」与「没配」必须区分：两者的下一步动作完全不同。"""
    monkeypatch.setattr(knowledge_search, "search", _no_hits)

    with knowledge_scope(uuid.uuid4()):
        result = await search_knowledge_base(None, "不相关的问题")

    assert "没有找到" in result
    assert "换个说法" in result
    # 不能把它说成"没有知识库"
    assert "没有关联知识库" not in result


async def test_tool_returns_formatted_hits(monkeypatch):
    hit = store.SearchHit(
        document_id="d1",
        document_title="退款政策",
        heading="条件",
        text="商品需完好。",
        score=0.9,
        vector_score=0.9,
        lexical_score=0.9,
    )

    async def fake_search(user_id, query, **kwargs):  # noqa: ANN001
        return [hit]

    monkeypatch.setattr(knowledge_search, "search", fake_search)

    with knowledge_scope(uuid.uuid4()):
        result = await search_knowledge_base(None, "退款")

    assert "《退款政策》" in result
    assert "商品需完好" in result
    # 不可信定界符要在（文档由用户上传，可能带注入）
    assert store.UNTRUSTED_HEADER in result


async def test_tool_truncates_overlong_query(monkeypatch):
    """模型偶尔把整段上下文当关键词发过来：截断比拒绝好。"""
    captured: list[str] = []

    async def fake_search(user_id, query, **kwargs):  # noqa: ANN001
        captured.append(query)
        return []

    monkeypatch.setattr(knowledge_search, "search", fake_search)

    with knowledge_scope(uuid.uuid4()):
        await search_knowledge_base(None, "长" * (MAX_QUERY_CHARS * 2))

    assert len(captured[0]) == MAX_QUERY_CHARS


async def test_tool_passes_current_user_to_search(monkeypatch):
    """作用域里的用户 id 必须原样传给检索——否则查的是别人的库。"""
    seen: list[uuid.UUID] = []
    target = uuid.uuid4()

    async def fake_search(user_id, query, **kwargs):  # noqa: ANN001
        seen.append(user_id)
        return []

    monkeypatch.setattr(knowledge_search, "search", fake_search)

    with knowledge_scope(target):
        await search_knowledge_base(None, "退款")

    assert seen == [target]


def test_tool_is_disabled_without_embedding_model(monkeypatch):
    """没配 embedding 就不该挂这个工具：挂上只会换回「未配置」，白烧一轮 token。"""
    monkeypatch.setattr(settings, "embedding_model", None)

    assert is_knowledge_base_enabled() is False


def test_tool_is_enabled_with_embedding_model():
    assert is_knowledge_base_enabled() is True


def test_scope_is_cleared_after_context_exit():
    """作用域必须能正确复位：泄漏会让下一轮（可能是另一个用户）查到别人的库。"""
    from app.tools.knowledge_scope import current_knowledge_scope

    user = uuid.uuid4()
    with knowledge_scope(user):
        assert current_knowledge_scope() == user
    assert current_knowledge_scope() is None


async def _no_hits(user_id, query, **kwargs):  # noqa: ANN001
    return []

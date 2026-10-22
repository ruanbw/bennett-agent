"""知识库（RAG）的请求 / 响应契约。

与同步协议同一个原则：**id 由服务端发**（这里是文档，不是会话）；删除只按
`(id, user_id)` 双条件，避免拿到别人 id 就能删（越权删除比越权读更糟——数据直接没了）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: 单篇文档的字符上限（与 `app/services/rag/store.py:MAX_DOCUMENT_CHARS` 对齐）。
#:
#: 这一层也校验一次而不是只靠服务层：入口处的 422 带字段名，用户能看懂；服务层抛的
#: `DocumentTooLarge` 是给业务流用的（后台任务）。
MAX_TEXT_CHARS = 200_000

#: 标题与来源的长度上限。标题会显示在引用里，太长会把引用行撑破。
MAX_TITLE_CHARS = 200
MAX_SOURCE_CHARS = 500


class DocumentCreate(BaseModel):
    """入库一篇文档（纯文本）。

    刻意**不**接受任意文件格式：PDF/Word 的解析是另一层责任（依赖、失败模式、
    格式兼容性都完全不同），而这一层要解决的是「文本进来之后怎么检索」。
    上传文件的形状可以由前端先抽成文本，也可以后续加一个独立的解析端点。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS, description="文档标题（引用时会显示）")
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS, description="文档正文（纯文本或 Markdown）")
    source: str | None = Field(default=None, max_length=MAX_SOURCE_CHARS, description="来源说明（文件名 / URL）")


class DocumentRead(BaseModel):
    """知识库文档的列表项。`chunk_count` 让用户知道它被切成了多少块。"""

    id: uuid.UUID
    title: str
    source: str | None
    chunk_count: int
    #: 生成向量时用的 embedding 模型。换模型后必须重建，否则维度对不上。
    embedding_model: str
    dimensions: int
    created_at: datetime


class DocumentListResponse(BaseModel):
    documents: list[DocumentRead]
    #: 文档数上限与当前用量：前端据此提示「还能再传几篇」。
    max_documents: int
    total_chunks: int


class KnowledgeCapabilities(BaseModel):
    """知识库是否可用，以及可用的原因/原因码。

    单独一种形状而不是塞进 `CapabilitiesResponse`：它与「工具是否装配」是同一件事
    的两面，但前端展示的位置不同（设置面板 vs 知识库管理页）。
    """

    enabled: bool = Field(description="是否配置了 EMBEDDING_MODEL（未配置时入库与检索都不可用）")
    embedding_model: str | None = Field(default=None, description="当前使用的 embedding 模型")
    #: 未启用时给用户看的原因（不是错误，是部署状态）。
    reason: str | None = Field(default=None, description="不可用的原因（配置项名与改法）")


class SearchRequest(BaseModel):
    """手动检索（调试用）：让用户在上传文档后自己验证检索效果。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=400, description="检索问题或关键词")
    top_k: int = Field(default=4, ge=1, le=8, description="返回条数")


class SearchHitRead(BaseModel):
    document_id: uuid.UUID
    document_title: str
    heading: str
    text: str
    score: float
    vector_score: float
    lexical_score: float


class SearchResponse(BaseModel):
    hits: list[SearchHitRead]


__all__ = [
    "MAX_SOURCE_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_TITLE_CHARS",
    "DocumentCreate",
    "DocumentListResponse",
    "DocumentRead",
    "KnowledgeCapabilities",
    "SearchHitRead",
    "SearchRequest",
    "SearchResponse",
]

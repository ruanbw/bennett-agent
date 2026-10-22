"""知识库（RAG）的 HTTP 接口：上传、列表、删除、手动检索。

## 权限边界

整个路由挂在 `current_active_user` 上：知识库是**用户私有**的，没有「共享知识库」
这个概念（管理员也看不到别人的文档）。所有查询与删除都带 `user_id` 条件，不靠
「先查出来再判断归属」——那样一次忘记判断就是越权。

## 为什么入库存文本而不是文件

文件解析（PDF/Word/HTML）是另一层责任：依赖体积、失败模式、格式兼容性都完全不同。
这一层要解决的是「文本进来之后怎么切、怎么检索」。前端可以先把文件抽成文本再调这里，
后续也能加一个独立的解析端点而不动检索逻辑。

## 未配置 embedding 时

入库与检索都会**明确拒绝**（409 + 原因），而不是静默成功：让用户以为文档传上去了、
实际上检索永远查不到，比直接说「这个部署没开知识库」糟得多。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.db import User
from app.schemas.knowledge import (
    DocumentCreate,
    DocumentListResponse,
    DocumentRead,
    KnowledgeCapabilities,
    SearchHitRead,
    SearchRequest,
    SearchResponse,
)
from app.services.auth import current_active_user
from app.services.rag.embeddings import embedding_model
from app.services.rag.store import (
    MAX_DOCUMENTS_PER_USER,
    DocumentTooLarge,
    KnowledgeError,
    TooManyDocuments,
    add_document,
    delete_document,
    document_stats,
    list_documents,
    search,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _capabilities() -> KnowledgeCapabilities:
    model = embedding_model()
    if model is None:
        return KnowledgeCapabilities(
            enabled=False,
            reason=(
                "服务端未配置 EMBEDDING_MODEL：知识库入库与检索都不可用。"
                "请在 .env 里配置一个 embedding 模型（地址与密钥复用默认提供商）。"
            ),
        )
    return KnowledgeCapabilities(enabled=True, embedding_model=model)


def _require_enabled() -> str:
    """知识库可用性断言；不可用时抛 409。

    用 409 而不是 422/503：请求本身没问题，是部署状态不支持——409 的语义是
    「与当前资源状态冲突」，正是这个意思，而且它不会被客户端当成「请求写错了」
    去重试同一个 payload。
    """
    model = embedding_model()
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_capabilities().reason or "知识库未启用",
        )
    return model


def _to_read(document) -> DocumentRead:  # noqa: ANN001 - ORM 对象
    return DocumentRead(
        id=document.id,
        title=document.title,
        source=document.source,
        chunk_count=document.chunk_count,
        embedding_model=document.embedding_model,
        dimensions=document.dimensions,
        created_at=document.created_at,
    )


@router.get(
    "/capabilities",
    response_model=KnowledgeCapabilities,
    summary="知识库是否可用（是否配置了 embedding 模型）",
    dependencies=[Depends(current_active_user)],
)
async def get_knowledge_capabilities() -> KnowledgeCapabilities:
    """未启用时给出原因与改法，而不是让上传按钮无声失效。"""
    return _capabilities()


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="列出当前用户的知识库文档",
    dependencies=[Depends(current_active_user)],
)
async def get_documents(user: User = Depends(current_active_user)) -> DocumentListResponse:  # noqa: B008
    """只列自己的文档：管理员也看不到别人的（知识库是用户私有的）。"""
    documents = await list_documents(user.id)
    _, total_chunks = await document_stats(user.id)
    return DocumentListResponse(
        documents=[_to_read(document) for document in documents],
        max_documents=MAX_DOCUMENTS_PER_USER,
        total_chunks=total_chunks,
    )


@router.post(
    "/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="上传一篇文档到知识库（纯文本 / Markdown）",
    dependencies=[Depends(current_active_user)],
)
async def create_document(request: DocumentCreate, user: User = Depends(current_active_user)) -> DocumentRead:  # noqa: B008
    """分块 → 向量化 → 落库。

    失败分类要点：超长与超配额都返回**带具体数字**的 400/413，而不是一句「入库失败」——
    用户需要知道该删多少、该拆成几篇。
    """
    _require_enabled()

    try:
        document = await add_document(
            user.id,
            title=request.title,
            text=request.text,
            source=request.source,
        )
    except DocumentTooLarge as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except TooManyDocuments as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except KnowledgeError as exc:
        # 内容为空这类：请求本身不合法，400
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - embedding 上游故障统一转成 502
        # 不把上游原文回给客户端：里面可能有网关地址与请求回显。日志里留全量。
        logger.exception("知识库入库失败 (user=%s, title=%s)", user.id, request.title)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="向量化服务暂时不可用，文档未能入库，请稍后重试。",
        ) from exc

    return _to_read(document)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除知识库文档（连同它的全部片段）",
    dependencies=[Depends(current_active_user)],
)
async def remove_document(document_id: str, user: User = Depends(current_active_user)) -> None:  # noqa: B008
    """按 `(id, user_id)` 双条件删。

    不存在与不属于自己都返回 404：区分两者等于告诉调用方「这个 id 确实存在，只是不
    是你的」——那本身就是一条不该泄露的信息。
    """
    import uuid

    try:
        parsed = uuid.UUID(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在") from exc

    if not await delete_document(user.id, parsed):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="手动检索知识库（调试用）",
    dependencies=[Depends(current_active_user)],
)
async def search_documents(request: SearchRequest, user: User = Depends(current_active_user)) -> SearchResponse:  # noqa: B008
    """让用户在上传后自己验证检索效果：命中什么、分数多少、靠哪一路。

    这比「相信它有用」重要——分块与检索的质量只能靠实际查询看出来，而对话里的检索
    是不可见的（模型可能根本没调那个工具）。
    """
    _require_enabled()
    # top_k 由请求指定（调试时想多看几条）；min_score 交给 settings 单点控制
    hits = await search(user.id, request.query, top_k=request.top_k)
    return SearchResponse(
        hits=[
            SearchHitRead(
                document_id=hit.document_id,
                document_title=hit.document_title,
                heading=hit.heading,
                text=hit.text,
                score=hit.score,
                vector_score=hit.vector_score,
                lexical_score=hit.lexical_score,
            )
            for hit in hits
        ]
    )


__all__ = ["router"]

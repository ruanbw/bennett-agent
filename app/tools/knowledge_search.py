"""知识库检索工具：让模型主动查用户上传的文档。

## 为什么是工具，而不是每轮自动注入

自动注入（每轮都检索、把结果塞进 instructions）看起来更"智能"，但它有两个问题：

1. **每轮都花钱**：一次 embedding 调用 + 一次全库扫描，而绝大多数消息（"你好"、
   "继续"）根本不需要知识库；
2. **污染上下文**：不相关的结果会挤占预算，还会让模型误以为这些是相关依据。

做成工具后，模型按提示词第 7 条自己判断"这个问题是否需要查资料"。代价是模型可能
该查而不查——所以提示词里对什么时候必须调用写得比较硬（见
`prompts/system_prompt.jinja` 的知识库一节）。

## user_id 从哪里来

工具签名里没有 `user_id` 参数：模型不该（也不能）指定查谁的库。当前用户通过
`ContextVar` 传进来（`app/tools/knowledge_scope.py`），由 `chat_service` 在每轮
开始时设置。这与 Tavily 缓存的租户隔离用的是同一套机制（`cache_scope.py`），
理由相同：工具是模型可调用的边界，任何"用户可以影响的身份"都不能由模型参数决定。

没有作用域时（CLI、测试）返回一句「当前会话没有关联知识库」——CLI 确实没有用户
概念，让它假装有知识库比说清楚更糟。
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext

from app.services.rag.embeddings import embedding_model
from app.services.rag.store import format_hits_for_model, search
from app.tools.knowledge_scope import current_knowledge_scope

logger = logging.getLogger(__name__)

#: 查询词的字符上限。模型偶尔会把整段上下文当关键词发过来；截断比拒绝好（截断后
#: 通常仍是一次有意义的检索），与 `tavily_search.normalize_query` 同一取舍。
MAX_QUERY_CHARS = 400


def is_knowledge_base_enabled() -> bool:
    """知识库是否可用（= 配了 embedding 模型）。

    这是工具装配的启用条件：没配 embedding 时挂上这个工具，模型调用它只会换回
    「未配置」——白烧一轮 token，与 `tavily_search` 没配 Key 时的情况完全一样。
    """
    return embedding_model() is not None


async def search_knowledge_base(ctx: RunContext[None], query: str) -> str:
    """在用户上传的知识库文档中检索相关内容。

    当用户的问题涉及他自己的文档、手册、规章制度、产品资料等**已经上传到本系统**的
    内容时调用它；不要用这个工具查公开网络信息（那是联网搜索工具的职责），也不要
    在没有任何上传文档时凭空调用来"确认一下"。

    query 应该是一个具体的检索问题或关键词组合（例如"退款申请的时限"），
    不要传入整段对话或与问题无关的追问。
    """
    scope = current_knowledge_scope()
    if not scope:
        return (
            "当前会话没有关联知识库（CLI 模式或未登录）。"
            "无法检索用户上传的文档；请直接依据对话内容回答，并说明你没有知识库可用。"
        )

    cleaned = " ".join((query or "").split())
    if not cleaned:
        return "检索关键词为空，没有执行检索。请给出具体的检索问题或关键词。"
    if len(cleaned) > MAX_QUERY_CHARS:
        cleaned = cleaned[:MAX_QUERY_CHARS]

    # 不传 top_k / min_score：`search()` 自己读 settings（单点定义，见它的 docstring）
    hits = await search(scope, cleaned)
    if not hits:
        # 说清是"没检索到"而不是"没有知识库"：两者的下一步动作完全不同
        # （前者该换个说法再查，后者该去上传文档）。
        return (
            f"知识库里没有找到与「{cleaned}」相关的内容。"
            "可以换个说法或关键词再查一次；如果仍然没有结果，说明用户还没上传相关资料，"
            "请直接依据对话回答并说明这一点，不要编造文档内容。"
        )

    logger.info(
        "知识库检索命中 %d 条（query=%s, top=%s）",
        len(hits),
        cleaned[:80],
        round(hits[0].score, 3),
    )
    return format_hits_for_model(hits)


__all__ = [
    "MAX_QUERY_CHARS",
    "is_knowledge_base_enabled",
    "search_knowledge_base",
]

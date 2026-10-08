"""Tavily 网页搜索工具。"""

import logging
from typing import Any

import httpx
from pydantic_ai import RunContext

from app.config import settings
from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
#: 搜索是交互路径上的一步，20 秒足够；再久不如让模型换个关键词。
TAVILY_TIMEOUT_SECONDS = 20.0

MAX_RESULTS_FLOOR = 1
MAX_RESULTS_CEILING = 10

#: 单条结果的正文上限与全文上限，防止过长内容占满模型上下文，
#: 也降低恶意网页把指令塞进上下文的成功率。
MAX_ITEM_CHARS = 600
MAX_TOTAL_CHARS = 6000
ERROR_BODY_CHARS = 200


async def tavily_search(
    ctx: RunContext[None],
    query: str,
    max_results: int = 5,
) -> str:
    """使用 Tavily 搜索引擎在互联网上实时搜索最新信息、新闻、网页资料等。

    Args:
        query: 搜索关键词或检索问题，如 'Python 3.13 最新特性' 或 'OpenAI 最新发布'
        max_results: 返回的最大结果数量（默认为 5）
    """
    api_key = settings.tavily_api_key
    if not api_key:
        return "错误: 未配置 Tavily API Key，无法执行联网搜索。"

    payload: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": max(MAX_RESULTS_FLOOR, min(max_results, MAX_RESULTS_CEILING)),
        "search_depth": "basic",
        "include_answer": True,
    }

    try:
        response = await get_http_client().post(
            TAVILY_SEARCH_URL,
            json=payload,
            timeout=TAVILY_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        # 超时与其它失败对模型是两种信号：前者值得重试或换关键词，后者不必
        logger.warning("Tavily 搜索超时 (query=%r)", query)
        return f"联网搜索超时，关键词: {query}"
    except Exception:
        # 工具失败要让模型看见原因并自行换策略，所以返回文案而不是抛出。
        # 细节进日志，不把上游异常原文塞进上下文（模型可能原样复述给用户）。
        logger.warning("Tavily 搜索请求失败 (query=%r)", query, exc_info=True)
        return f"联网搜索发生异常，关键词: {query}"

    if response.status_code != 200:
        if response.status_code == 401:
            return "联网搜索认证失败 (HTTP 401): Tavily API Key 无效或未授权。"
        if response.status_code == 429:
            return "联网搜索请求过于频繁或配额耗尽 (HTTP 429)，请稍后重试。"

        body = response.text[:ERROR_BODY_CHARS]
        return f"联网搜索请求失败 (HTTP {response.status_code}): {body}"

    return _format_results(response.json(), query)


def _format_results(data: dict[str, Any], query: str) -> str:
    """把 Tavily 响应压成给模型看的纯文本，并对长度设上限。"""
    lines: list[str] = []

    direct_answer = data.get("answer")
    if direct_answer:
        lines.append(f"【快速摘要】: {direct_answer}\n")

    results = data.get("results") or []
    if not results:
        lines.append("未检索到相关网页内容。")
    else:
        lines.append("【搜索结果】:")
        for index, item in enumerate(results, start=1):
            title = item.get("title", "无标题")
            url = item.get("url", "")
            content = (item.get("content") or "").strip()
            if len(content) > MAX_ITEM_CHARS:
                content = content[:MAX_ITEM_CHARS] + "... [已截断]"
            lines.append(f"{index}. {title}\n   链接: {url}\n   内容: {content}\n")

    full_text = "\n".join(lines)
    if len(full_text) > MAX_TOTAL_CHARS:
        full_text = full_text[:MAX_TOTAL_CHARS] + "\n... [搜索全文超出上限，已截断]"
    return full_text

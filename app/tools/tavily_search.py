"""Tavily 网页搜索工具"""
import os
from typing import Any, Dict, Optional
import httpx
from pydantic_ai import RunContext

from app.config import settings


def _resolve_tavily_api_key() -> Optional[str]:
    """优先从 settings 读取，若无则尝试从环境变量读取"""
    if getattr(settings, "tavily_api_key", None):
        return settings.tavily_api_key

    return os.getenv("TAVILY_API_KEY")


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
    api_key = _resolve_tavily_api_key()
    if not api_key:
        return "错误: 未配置 Tavily API Key，无法执行联网搜索。"

    url = "https://api.tavily.com/search"
    payload: Dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "max_results": max(1, min(max_results, 10)),
        "search_depth": "basic",
        "include_answer": True,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                if response.status_code == 401:
                    return "联网搜索认证失败 (HTTP 401): Tavily API Key 无效或未授权。"
                if response.status_code == 429:
                    return "联网搜索请求过于频繁或配额耗尽 (HTTP 429)，请稍后重试。"
                err_text = response.text[:200] + ("..." if len(response.text) > 200 else "")
                return f"联网搜索请求失败 (HTTP {response.status_code}): {err_text}"

            data = response.json()
            lines = []

            direct_answer = data.get("answer")
            if direct_answer:
                lines.append(f"【快速摘要】: {direct_answer}\n")

            results = data.get("results") or []
            if not results:
                lines.append("未检索到相关网页内容。")
            else:
                lines.append("【搜索结果】:")
                for idx, item in enumerate(results, start=1):
                    title = item.get("title", "无标题")
                    item_url = item.get("url", "")
                    content = item.get("content", "").strip()
                    # 每条结果正文截断，防御过长内容占满模型上下文及恶意网页注入
                    if len(content) > 600:
                        content = content[:600] + "... [已截断]"
                    lines.append(f"{idx}. {title}\n   链接: {item_url}\n   内容: {content}\n")

            full_text = "\n".join(lines)
            # 全文总长度设上限（约 6000 字符，控制在 2k-3k token 以内）
            if len(full_text) > 6000:
                full_text = full_text[:6000] + "\n... [搜索全文超出上限，已截断]"
            return full_text

    except httpx.TimeoutException:
        return f"联网搜索超时，关键词: {query}"
    except Exception as e:
        return f"联网搜索发生异常: {str(e)}"

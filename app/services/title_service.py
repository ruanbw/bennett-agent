"""会话标题生成：用一次 LLM 调用给会话起名。

标题只是装饰（前端 fire-and-forget、失败保留占位），所以失败一律退回首行截断，不冒泡给用户。
"""

import logging

from app.services.agent_factory import TITLE_TIMEOUT_SECONDS, build_title_agent
from app.services.model_service import ChatModelSnapshot
from app.services.title_text import fallback_title, normalize_title

logger = logging.getLogger(__name__)

MAX_TITLE_INPUT_CHARS = 500
MAX_TITLE_CHARS = 20
FALLBACK_TITLE_CHARS = 15
DEFAULT_TITLE = "新对话"


async def generate_title_from_prompt(snapshot: ChatModelSnapshot, prompt: str) -> str:
    """用 LLM 为会话生成简短标题，失败时回退到首行截断——标题只是装饰，不该让用户看到报错。

    清洗与截断在 `title_text`（纯函数）：不剥「标题：」前缀的话，它会原样出现在侧边栏。
    """
    model_name = snapshot.key
    try:
        if snapshot.target is None and snapshot.registry_managed:
            raise RuntimeError(f"模型不可用: {model_name}")
        agent = build_title_agent(model_name, target=snapshot.target)
        result = await agent.run(
            f"请为以下内容生成简短标题：\n{prompt[:MAX_TITLE_INPUT_CHARS]}",
            # 标题是装饰：预算远小于流式空闲超时，避免网关卡住时白占两分钟
            model_settings={"timeout": TITLE_TIMEOUT_SECONDS},
        )
        title = normalize_title(result.output, MAX_TITLE_CHARS)
        if title:
            return title
        logger.info("标题模型返回空内容，回退到首行截断 (model=%s)", model_name)
    except Exception:
        logger.warning("生成会话标题失败，回退到截断首行 (model=%s)", model_name, exc_info=True)

    return fallback_title(prompt, FALLBACK_TITLE_CHARS) or DEFAULT_TITLE

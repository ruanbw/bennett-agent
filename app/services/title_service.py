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

#: 用户原文的定界符：它与 system instruction 里的「只当数据」一句成对出现。
TITLE_INPUT_TAG = "user_message"


def _title_model_settings(snapshot: ChatModelSnapshot) -> dict[str, object]:
    """标题这一次调用的运行期参数：超时，以及（仅在模型声明支持时）关掉思考。

    思考开关不能无条件传：不认 `reasoning_effort` 的模型会因此 400，标题随即静默退回截断。
    供应商同步进库的 `reasoning_levels` 含 `none` 才说明它接受「关闭思考」。
    """
    model_settings: dict[str, object] = {"timeout": TITLE_TIMEOUT_SECONDS}
    if "none" in snapshot.reasoning_levels:
        model_settings["extra_body"] = {"reasoning_effort": "none"}
    return model_settings


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
            # 用户原文是不可信内容：用定界符圈起来，system instruction 里声明「只当数据」。
            # 仓库其它不可信入口（Tavily、MCP 结果、摘要 digest）都做了定界，这里没有理由例外。
            f"请为以下内容生成简短标题：\n<{TITLE_INPUT_TAG}>\n{prompt[:MAX_TITLE_INPUT_CHARS]}\n</{TITLE_INPUT_TAG}>",
            # 标题是装饰：预算远小于流式空闲超时，避免网关卡住时白占两分钟
            model_settings=_title_model_settings(snapshot),
        )
        title = normalize_title(result.output, MAX_TITLE_CHARS)
        if title:
            return title
        logger.info("标题模型返回空内容，回退到首行截断 (model=%s)", model_name)
    except Exception:
        logger.warning("生成会话标题失败，回退到截断首行 (model=%s)", model_name, exc_info=True)

    return fallback_title(prompt, FALLBACK_TITLE_CHARS) or DEFAULT_TITLE

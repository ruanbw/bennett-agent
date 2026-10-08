"""唯一的 Agent 装配点。

R-3：改动前有两个工厂——`main.create_agent()` 与 `chat_service.get_agent_for_model()`。
两者都构造 `LiteLLMProvider` + `OpenAIChatModel` + `Agent`，但前者丢掉了
`reasoning_effort` 与 `model_settings`。同一份配置，CLI 与 Web 因此跑出不同的 agent，
而差异只在人读两份代码时才看得见。

`generate_title_from_prompt` 里还有第三份 provider 构造。现在所有接入点都经过本模块。

O-2：装配结果按参数缓存。pydantic-ai 的 `Agent` 设计上就是可复用的（常见用法是模块级
建一个），而此前每条消息都要重建 provider + model + agent。
"""

import logging
from functools import lru_cache

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider

from app.config import settings
from app.prompts import prompt_manager
from app.tools import ALL_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = "system_prompt.jinja"

TITLE_SYSTEM_INSTRUCTION = (
    "你是一个会话标题生成助手。根据用户的问题或指令，概括出极简的会话标题。"
    "要求：1. 紧扣核心意向；2. 不超过10个汉字或5个英文单词；"
    "3. 不要包含任何标点符号、引号或前缀解释；4. 纯文本直接返回标题。"
)

#: 缓存条目上限。远大于实际用到的模型数，同时给「客户端乱传 model 名」兜底。
AGENT_CACHE_SIZE = 32

#: 建连 / 写 / 取连接的硬上限。它们不需要可配置：超出这些值已经说明网络或上游异常。
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
UPSTREAM_WRITE_TIMEOUT_SECONDS = 30.0
UPSTREAM_POOL_TIMEOUT_SECONDS = 10.0

#: 明确要求关闭思考时各网关接受的写法不同，统一归一化成 "none"。
_DISABLED_EFFORT_ALIASES = frozenset({"off", "none", "false"})


def normalize_reasoning_effort(reasoning_effort: str | None) -> str | None:
    """归一化思考等级。

    返回 `None` 表示不向上游传该参数（让网关用默认值）。这是思考等级归一化的
    唯一权威实现；前端侧的对应收敛见 CODE_REVIEW.md R-7。
    """
    if not reasoning_effort:
        return None
    effort = reasoning_effort.strip().lower()
    if not effort:
        return None
    return "none" if effort in _DISABLED_EFFORT_ALIASES else effort


def build_provider() -> LiteLLMProvider:
    """构造 LiteLLM provider。

    所有模型接入点共用，避免 base_url 与密钥兜底逻辑第三次分叉。
    """
    return LiteLLMProvider(
        api_base=settings.litellm_api_base,
        api_key=settings.litellm_api_key or "sk-no-key",
    )


def build_upstream_timeout() -> httpx.Timeout | None:
    """上游调用的超时配置；配置为 0 时返回 None（不限制）。

    为什么必须有超时：网关接受连接但永不出数据时（实测本机的 LiteLLM 代理就是这样，
    `/v1/chat/completions` 会无限挂起），没有超时的后果是——连接一直被占住、客户端只
    收到 SSE ping、服务端**一行日志都没有**，而生成路径永远不会走到 except 分支。

    `read` 用「空闲」语义（多久没收到数据），而不是总时长，所以正常的长时间流式输出
    不会被提前掉断。
    """
    idle = settings.model_upstream_timeout
    if idle <= 0:
        return None

    return httpx.Timeout(
        connect=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
        read=idle,
        write=UPSTREAM_WRITE_TIMEOUT_SECONDS,
        pool=UPSTREAM_POOL_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=AGENT_CACHE_SIZE)
def build_agent(model_name: str, reasoning_effort: str | None = None) -> Agent:  # noqa: ANN401
    """装配主对话 Agent。CLI 与 Web 都必须走这里，否则两者会再次漂移。"""
    system_prompt = prompt_manager.render(
        SYSTEM_PROMPT_TEMPLATE,
        app_env=settings.app_env,
        assistant_name=settings.assistant_name,
    )

    effort = normalize_reasoning_effort(reasoning_effort)
    model_settings: dict[str, object] = {}
    if effort:
        model_settings["extra_body"] = {"reasoning_effort": effort}

    timeout = build_upstream_timeout()
    if timeout is not None:
        model_settings["timeout"] = timeout

    model = OpenAIChatModel(
        model_name=model_name,
        provider=build_provider(),
        settings=model_settings or None,
    )

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=ALL_TOOLS,
    )


@lru_cache(maxsize=AGENT_CACHE_SIZE)
def build_title_agent(model_name: str) -> Agent:  # noqa: ANN401
    """装配会话标题生成 Agent：另一个 system prompt，且不挂工具。"""
    timeout = build_upstream_timeout()
    model = OpenAIChatModel(
        model_name=model_name,
        provider=build_provider(),
        settings={"timeout": timeout} if timeout is not None else None,
    )
    return Agent(
        model=model,
        system_prompt=TITLE_SYSTEM_INSTRUCTION,
    )


def reset_agent_cache() -> None:
    """清空装配缓存。供测试与「改了配置想立刻生效」的场景使用。"""
    build_agent.cache_clear()
    build_title_agent.cache_clear()

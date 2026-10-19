"""唯一的 Agent 装配点。

R-3：改动前有两个工厂——`main.create_agent()` 与 `chat_service.get_agent_for_model()`。
两者都构造 `LiteLLMProvider` + `OpenAIChatModel` + `Agent`，但前者丢掉了
`reasoning_effort` 与 `model_settings`。同一份配置，CLI 与 Web 因此跑出不同的 agent，
而差异只在人读两份代码时才看得见。

`generate_title_from_prompt` 里还有第三份 provider 构造。现在所有接入点都经过本模块。

O-2：装配结果按参数缓存。pydantic-ai 的 `Agent` 设计上就是可复用的（常见用法是模块级
建一个），而此前每条消息都要重建 provider + model + agent。

缓存的键是「归一化后的参数 + settings 派生值」：不这么做时，客户端换个大小写就能各占
一个缓存槽（把真实条目挤出去），运维改了 .env 也只能重启进程才生效。
"""

import logging
from collections.abc import Callable
from functools import lru_cache

import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider

from app.config import settings
from app.config.proxy_env import sanitize_no_proxy_env
from app.prompts import prompt_manager
from app.services.fallback_tracker import record_fallback
from app.services.upstream_errors import is_fallback_eligible
from app.tools import build_tools
from app.tools.guard import ToolErrorIsolation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = "system_prompt.jinja"

TITLE_SYSTEM_INSTRUCTION = (
    "你是一个会话标题生成助手。根据用户的问题或指令，概括出极简的会话标题。"
    "要求：1. 紧扣核心意向；2. 不超过10个汉字或5个英文单词；"
    "3. 不要包含任何标点符号、引号或前缀解释；4. 纯文本直接返回标题。"
)

#: 缓存条目上限。远大于实际用到的模型数，同时给「客户端乱传 model 名」兜底。
AGENT_CACHE_SIZE = 32

#: 工具异常隔离能力。无状态，所有缓存条目共用同一个实例即可。
#: 没有它时，任意工具抛出的非 `ModelRetry` 异常都会中止整轮生成（见 `app/tools/guard.py`）。
_TOOL_ERROR_ISOLATION = ToolErrorIsolation()

#: 建连 / 写 / 取连接的硬上限。它们不需要可配置：超出这些值已经说明网络或上游异常。
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
UPSTREAM_WRITE_TIMEOUT_SECONDS = 30.0
UPSTREAM_POOL_TIMEOUT_SECONDS = 10.0

#: 明确要求关闭思考时各网关接受的写法不同，统一归一化成 "none"。
#: 取值与前端 `frontend/src/lib/reasoning-effort.ts:OFF_ALIASES` 对齐：两边不一致
#: 会出现「界面显示已关闭、实际仍开着思考」这种不报错的错位。
_DISABLED_EFFORT_ALIASES = frozenset({"off", "none", "false", "disabled"})


def normalize_reasoning_effort(reasoning_effort: str | None) -> str | None:
    """归一化思考等级。

    返回 `None` 表示不向上游传该参数（让网关用默认值）。这是思考等级归一化的
    唯一权威实现；前端侧的对应收敛见 CODE_REVIEW.md R-7。

    不在别名表里的取值（例如 `xhigh` / `max` / `minimal` 这类网关自定义等级）原样
    透传：等级词表由各网关决定，这里只做大小写与空白的收敛。
    """
    if not reasoning_effort:
        return None
    effort = reasoning_effort.strip().lower()
    if not effort:
        return None
    return "none" if effort in _DISABLED_EFFORT_ALIASES else effort


def _settings_cache_key() -> tuple[object, ...]:
    """settings 中会影响装配结果的派生值，参与装配缓存键。

    没有它时缓存只认 `(model_name, effort)`：运维改了 `.env`（base_url / 助手名 /
    超时 / tavily key）后，只要进程没重启就仍在用旧 agent，表现为「配置明明改了却
    没生效」。代价是每改一次配置就多占一个缓存槽，旧条目要等 LRU 淘汰——缓存上限
    32，相对收益可以忽略。tavily 只取布尔值，避免把密钥原文放进键里。
    """
    return (
        settings.litellm_api_base,
        settings.app_env,
        settings.assistant_name,
        settings.model_upstream_timeout,
        # 备用模型会影响装配结果；只在键里放名字（不含密钥）。
        settings.fallback_model_name,
        bool(settings.tavily_api_key),
    )


def build_provider() -> LiteLLMProvider:
    """构造 LiteLLM provider。

    所有模型接入点共用，避免 base_url 与密钥兜底逻辑第三次分叉。

    这里显式关闭 SDK 的自动重试（openai 默认 `max_retries=2`）：网关（LiteLLM）
    自己就会重试上游，两层重试叠加空闲超时会把单轮对话的最坏耗时乘上去。pydantic-ai
    2.31.1 的 `LiteLLMProvider` 没有 `max_retries` 参数，只能自己构造 `AsyncOpenAI`
    再通过官方支持的 `openai_client=` 传入（见其 `__init__` 重载）。
    """
    # AsyncOpenAI 内部会自建 httpx 客户端并继承环境里的代理设置；坏 no_proxy 会让它
    # 在构造阶段就抛 InvalidURL（见 app/config/proxy_env.py），因此先清理再构造。
    sanitize_no_proxy_env()
    client = AsyncOpenAI(
        base_url=settings.litellm_api_base,
        api_key=settings.litellm_api_key or "sk-no-key",
        max_retries=0,
    )
    return LiteLLMProvider(openai_client=client)


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


def _configured_fallback_name(primary_name: str) -> str | None:
    """取配置里的备用模型名；空值、与主模型相同都视为「不启用」。

    同名时没有必要包 `FallbackModel`：它只会对同一个模型再打一次，还让错误变成
    异常组（分类文案反而更绕）。大小写不归一：模型 id 由网关定义，后端不做猜测。
    """
    fallback_name = (settings.fallback_model_name or "").strip()
    if not fallback_name or fallback_name == primary_name:
        return None
    return fallback_name


def _build_fallback_handler(primary_name: str, fallback_name: str) -> Callable[[Exception], bool]:
    """`FallbackModel` 的切换判定：复用上游错误分类，只切「换模型可能救回来」的失败。

    不切的情况：401/403（同一套网关凭据，备用模型也会失败）、400/404（请求或模型
    本身的问题）——这些按分类文案直接告诉用户，避免无意义地多打一次上游。

    日志放在这里而不是最终异常处：`FallbackExceptionGroup` 只说「都失败了」，
    不会告诉你主模型当时是因为什么失败。
    """

    def should_fallback(error: Exception) -> bool:
        if not is_fallback_eligible(error):
            return False
        logger.warning(
            "模型调用失败，尝试备用模型 (primary=%s, fallback=%s)",
            primary_name,
            fallback_name,
            exc_info=True,
        )
        # 只有真的切换才记录：对话流会把它翻译成 `fallback` 事件告诉用户本轮是谁答的。
        record_fallback(primary_name, fallback_name)
        return True

    return should_fallback


def _build_chat_model(model_name: str, model_settings: dict[str, object]) -> Model:
    """构造主对话模型，配置了备用模型时包一层 `FallbackModel`。

    兜底顺序：`FallbackModel` 先在**流打开阶段**切备用模型（pydantic-ai 的
    `request_stream` 语义：中途失败不接管，因此不会出现「正文已流出又重来一遍」）；
    备用模型也失败后，`chat_service` 的瞬时错误重试再整轮试一次。

    备用模型与主模型共用同样的 `model_settings`（思考等级、超时）——它必须能接受
    这些参数，否则切换本身就会 400。这条约束写进了 `Settings.fallback_model_name`。
    """
    primary = OpenAIChatModel(
        model_name=model_name,
        provider=build_provider(),
        settings=model_settings or None,
    )

    fallback_name = _configured_fallback_name(model_name)
    if fallback_name is None:
        return primary

    fallback = OpenAIChatModel(
        model_name=fallback_name,
        provider=build_provider(),
        settings=model_settings or None,
    )
    return FallbackModel(primary, fallback, fallback_on=_build_fallback_handler(model_name, fallback_name))


def render_system_prompt() -> str:
    """渲染主对话的系统提示词。

    独立成函数是因为它有两个消费方：装配（`_build_agent_cached`）与上下文预算
    （`token_budget` 要把它算进每轮的固定开销）。两处各写一遍 render 参数，
    改了其中一处就会出现「预算按 A 算、实际发出去的是 B」。
    """
    return prompt_manager.render(
        SYSTEM_PROMPT_TEMPLATE,
        app_env=settings.app_env,
        assistant_name=settings.assistant_name,
    )


@lru_cache(maxsize=AGENT_CACHE_SIZE)
def _build_agent_cached(model_name: str, effort: str | None, cache_key: tuple[object, ...]) -> Agent:  # noqa: ANN401
    """装配主对话 Agent 的缓存实现，只应由 `build_agent` 调用。

    `effort` 必须已经归一化、`cache_key` 必须来自 `_settings_cache_key()`：
    两者都只在缓存键里起作用，用来保证「等价的输入命中同一个槽位」与
    「配置变了就换槽位」。
    """
    system_prompt = render_system_prompt()

    model_settings: dict[str, object] = {}
    if effort:
        model_settings["extra_body"] = {"reasoning_effort": effort}

    timeout = build_upstream_timeout()
    if timeout is not None:
        model_settings["timeout"] = timeout

    model = _build_chat_model(model_name, model_settings)

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=build_tools(),
        # 工具是外部边界：一个工具抛异常不应该让整轮回答作废。
        # 兜底只作用于「漏网抛出来」的异常，工具自己返回文案的路径不受影响。
        capabilities=[_TOOL_ERROR_ISOLATION],
    )


def build_agent(model_name: str, reasoning_effort: str | None = None) -> Agent:  # noqa: ANN401
    """装配主对话 Agent。CLI 与 Web 都必须走这里，否则两者会再次漂移。

    归一化必须在进入缓存**之前**完成：否则 `"HIGH"` / `"high"` / `" high "` 会各占
    一个缓存槽，客户端换个大小写或空格就能把 32 个槽位挤满，把真实条目淘汰掉。
    """
    return _build_agent_cached(model_name, normalize_reasoning_effort(reasoning_effort), _settings_cache_key())


@lru_cache(maxsize=AGENT_CACHE_SIZE)
def _build_title_agent_cached(model_name: str, cache_key: tuple[object, ...]) -> Agent:  # noqa: ANN401
    """装配会话标题生成 Agent 的缓存实现，只应由 `build_title_agent` 调用。

    `cache_key` 只用于缓存失效；标题 agent 只用得到其中的 base_url 与超时，
    但共用同一个键更简单，多失效一次没有代价。
    """
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


def build_title_agent(model_name: str) -> Agent:  # noqa: ANN401
    """装配会话标题生成 Agent：另一个 system prompt，且不挂工具。"""
    return _build_title_agent_cached(model_name, _settings_cache_key())


def reset_agent_cache() -> None:
    """清空装配缓存。供测试与「改了配置想立刻生效」的场景使用。"""
    _build_agent_cached.cache_clear()
    _build_title_agent_cached.cache_clear()

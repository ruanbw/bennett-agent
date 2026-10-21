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
from collections.abc import Callable, Sequence
from functools import lru_cache

import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider

from app.config import is_usable_api_key, settings
from app.config.proxy_env import sanitize_no_proxy_env
from app.prompts import prompt_manager
from app.services.fallback_config import configured_fallback_name
from app.services.fallback_tracker import record_fallback
from app.services.model_target import ModelTarget
from app.services.upstream_errors import describe_fallback_reason, is_fallback_eligible
from app.tools import build_tools
from app.tools.guard import ToolErrorIsolation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = "system_prompt.jinja"

#: 联网搜索工具名。提示词里「可以联网」这句话与它的装配状态绑定，单点定义避免拼错。
WEB_SEARCH_TOOL_NAME = "tavily_search"
#: 澄清工具名。提示词的第 3 条同样与它是否挂载绑定：没挂时教模型「调用 ask_user_clarification」
#: 只会换回一次「工具不存在」，白烧一轮 token。
CLARIFICATION_TOOL_NAME = "ask_user_clarification"
#: 当前时间工具名。纯本地计算、目前始终挂载，但提示词仍然按真实装配渲染——
#: 「模板里写死的工具名」正是第 4 条修过的那类漂移。
CURRENT_TIME_TOOL_NAME = "get_current_time"

#: 标题生成的上游预算（秒）。
#:
#: 标题只是装饰：网关/上游卡住时，`/api/chat/title` 没必要跟着等 120 秒的流式空闲超时——
#: 那会把一个请求（以及一个限流槽）占住两分钟，而用户根本不关心标题什么时候回来。
#: 前端本来就是 fire-and-forget + 失败保留占位标题，10 秒够了。
TITLE_TIMEOUT_SECONDS = 10.0

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
    唯一权威实现；前端侧的对应收敛见 `frontend/src/lib/reasoning-effort.ts`。

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
        # 与 `build_tools()` 的启用判定同源：占位 Key 视为未配置，避免缓存键在
        # 「配置不可用」与「已配置」之间反复切换而装配结果其实完全相同。
        is_usable_api_key(settings.tavily_api_key),
    )


def _build_upstream_http_client() -> httpx.AsyncClient:
    """对话链路的 httpx 客户端：**不跟随重定向**。

    `openai` SDK 默认 `follow_redirects=True`（`_base_client.py` 里
    `kwargs.setdefault("follow_redirects", True)`），而 `outbound_guard` 只校验
    管理员填写的 base_url 首跳。如果该主机返回 307/303，SDK 会自行把请求发到
    `Location` 指向的内网/云元数据地址，守卫完全看不到（M-02）。

    这里显式关闭跟随。需要重定向的合法网关极少；要支持时应逐跳重新执行 scheme +
    地址校验，而不是交给 SDK 静默跟随。
    """
    timeout = build_upstream_timeout()
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout if timeout is not None else None,
    )


def build_provider(target: ModelTarget | None = None) -> LiteLLMProvider:
    """构造 LiteLLM provider。

    所有模型接入点共用，避免 base_url 与密钥兜底逻辑第三次分叉。`target=None` 表示
    尚未接入管理后台的旧路径，取环境变量；否则取该提供商自己的端点与凭据。

    这里显式关闭 SDK 的自动重试（openai 默认 `max_retries=2`）：网关（LiteLLM）
    自己就会重试上游，两层重试叠加空闲超时会把单轮对话的最坏耗时乘上去。pydantic-ai
    2.31.1 的 `LiteLLMProvider` 没有 `max_retries` 参数，只能自己构造 `AsyncOpenAI`
    再通过官方支持的 `openai_client=` 传入（见其 `__init__` 重载）。

    同时注入一个**不跟随重定向**的 httpx 客户端（见 `_build_upstream_http_client`）。
    """
    base_url = target.base_url if target is not None else settings.litellm_api_base
    api_key = target.api_key if target is not None else settings.litellm_api_key

    # AsyncOpenAI 内部会自建 httpx 客户端并继承环境里的代理设置；坏 no_proxy 会让它
    # 在构造阶段就抛 InvalidURL（见 app/config/proxy_env.py），因此先清理再构造。
    sanitize_no_proxy_env()
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "sk-no-key",
        max_retries=0,
        http_client=_build_upstream_http_client(),
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
    """取配置里的备用模型名（口径与 `model_service` 共用 `fallback_config`）。"""
    return configured_fallback_name(primary_name)


def _build_fallback_handler(
    primary_label: str,
    fallback_label: str,
    primary_name: str | None = None,
    fallback_name: str | None = None,
) -> Callable[[Exception], bool]:
    """`FallbackModel` 的切换判定：复用上游错误分类，只切「换模型可能救回来」的失败。

    `*_label` 是给事件/用量用的对外标识（数据库路径下是路由键），`*_name` 是发给
    上游的模型名（只用于日志，缺省时与 label 相同）。两者分开是因为用量事件要按
    **路由键**再查一次窗口。

    不切的情况：401/403（同一套网关凭据，备用模型也会失败）、400/404（请求或模型
    本身的问题）——这些按分类文案直接告诉用户，避免无意义地多打一次上游。

    日志放在这里而不是最终异常处：`FallbackExceptionGroup` 只说「都失败了」，
    不会告诉你主模型当时是因为什么失败。
    """
    primary_name = primary_name or primary_label
    fallback_name = fallback_name or fallback_label

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
        # 原因一起记：只说「已切换」不说为什么，用户与运维都只能猜。
        record_fallback(primary_label, fallback_label, describe_fallback_reason(error))
        return True

    return should_fallback


def _build_chat_model(
    model_name: str,
    model_settings: dict[str, object],
    target: ModelTarget | None = None,
) -> Model:
    """构造主对话模型，配置了备用模型时包一层 `FallbackModel`。

    兜底顺序：`FallbackModel` 先在**流打开阶段**切备用模型（pydantic-ai 的
    `request_stream` 语义：中途失败不接管，因此不会出现「正文已流出又重来一遍」）；
    备用模型也失败后，`chat_service` 的瞬时错误重试再整轮试一次。

    备用模型与主模型共用同样的 `model_settings`（思考等级、超时）——它必须能接受
    这些参数，否则切换本身就会 400。数据库路径下备用模型被限制在**同一提供商**内
    （见 `model_service.resolve_model_target`）。
    """
    if target is not None:
        primary = OpenAIChatModel(
            model_name=target.upstream_model,
            provider=build_provider(target),
            settings=model_settings or None,
        )
        fallback = target.fallback
        if fallback is None:
            return primary

        fallback_model = OpenAIChatModel(
            model_name=fallback.upstream_model,
            provider=build_provider(fallback),
            settings=model_settings or None,
        )
        return FallbackModel(
            primary,
            fallback_model,
            fallback_on=_build_fallback_handler(
                target.key,
                fallback.key,
                target.upstream_model,
                fallback.upstream_model,
            ),
        )

    primary = OpenAIChatModel(
        model_name=model_name,
        provider=build_provider(),
        settings=model_settings or None,
    )

    fallback_name = _configured_fallback_name(model_name)
    if fallback_name is None:
        return primary

    fallback_model = OpenAIChatModel(
        model_name=fallback_name,
        provider=build_provider(),
        settings=model_settings or None,
    )
    return FallbackModel(
        primary,
        fallback_model,
        fallback_on=_build_fallback_handler(model_name, fallback_name, model_name, fallback_name),
    )


def render_system_prompt(tool_names: Sequence[str] | None = None) -> str:
    """渲染主对话的系统提示词。

    独立成函数是因为它有两个消费方：装配（`_build_agent_cached`）与上下文预算
    （`token_budget` 要把它算进每轮的固定开销）。两处各写一遍 render 参数，
    改了其中一处就会出现「预算按 A 算、实际发出去的是 B」。

    `tool_names` 默认取**当前实际装配**的工具列表：提示词里「可以联网搜索」这句话
    必须与 `build_tools()` 的结果一致。此前模板无条件写着「请调用 tavily_search」，
    没配 Key 时模型会去调一个根本不存在的工具——白烧一轮 token，还让用户看到
    「工具不存在」的错误。把装配结果渲染进提示词，两者就不会再漂移。
    """
    names = list(tool_names) if tool_names is not None else [tool.name for tool in build_tools()]
    return prompt_manager.render(
        SYSTEM_PROMPT_TEMPLATE,
        app_env=settings.app_env,
        assistant_name=settings.assistant_name,
        tools=names,
        web_search=WEB_SEARCH_TOOL_NAME in names,
        clarification=CLARIFICATION_TOOL_NAME in names,
        current_time=CURRENT_TIME_TOOL_NAME in names,
    )


@lru_cache(maxsize=AGENT_CACHE_SIZE)
def _build_agent_cached(
    model_name: str,
    effort: str | None,
    cache_key: tuple[object, ...],
    target: ModelTarget | None,
) -> Agent:  # noqa: ANN401
    """装配主对话 Agent 的缓存实现，只应由 `build_agent` 调用。

    `effort` 必须已经归一化、`cache_key` 必须来自 `_settings_cache_key()`、`target`
    来自 `model_service.resolve_model_target()`：三者都参与缓存键，用来保证「等价的
    输入命中同一个槽位」与「配置变了就换槽位」。`ModelTarget` 是 frozen dataclass，
    本身可哈希；管理员改了地址/密钥/模型后会产生新的 target，旧缓存自然不再命中。
    """
    # 只装配一次工具：提示词里的工具清单必须与真正挂上去的那份是同一份，
    # 分两次 `build_tools()` 调用在配置热更新时可能拿到不同结果。
    tools = build_tools()
    system_prompt = render_system_prompt([tool.name for tool in tools])

    model_settings: dict[str, object] = {}
    if effort:
        model_settings["extra_body"] = {"reasoning_effort": effort}

    timeout = build_upstream_timeout()
    if timeout is not None:
        model_settings["timeout"] = timeout

    model = _build_chat_model(model_name, model_settings, target)

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        # 工具是外部边界：一个工具抛异常不应该让整轮回答作废。
        # 兜底只作用于「漏网抛出来」的异常，工具自己返回文案的路径不受影响。
        capabilities=[_TOOL_ERROR_ISOLATION],
    )


def build_agent(
    model_name: str,
    reasoning_effort: str | None = None,
    *,
    target: ModelTarget | None = None,
) -> Agent:  # noqa: ANN401
    """装配主对话 Agent。CLI 与 Web 都必须走这里，否则两者会再次漂移。

    归一化必须在进入缓存**之前**完成：否则 `"HIGH"` / `"high"` / `" high "` 会各占
    一个缓存槽，客户端换个大小写或空格就能把 32 个槽位挤满，把真实条目淘汰掉。

    `target` 由调用方先解析（异步、查库）；`None` 表示旧版环境变量路径。
    """
    return _build_agent_cached(
        model_name,
        normalize_reasoning_effort(reasoning_effort),
        _settings_cache_key(),
        target,
    )


@lru_cache(maxsize=AGENT_CACHE_SIZE)
def _build_title_agent_cached(
    model_name: str,
    cache_key: tuple[object, ...],
    target: ModelTarget | None,
) -> Agent:  # noqa: ANN401
    """装配会话标题生成 Agent 的缓存实现，只应由 `build_title_agent` 调用。

    `cache_key` 只用于缓存失效；标题 agent 只用得到其中的超时与提供商配置，
    但共用同一个键更简单，多失效一次没有代价。
    """
    timeout = build_upstream_timeout()
    upstream_name = target.upstream_model if target is not None else model_name
    model = OpenAIChatModel(
        model_name=upstream_name,
        provider=build_provider(target),
        settings={"timeout": timeout} if timeout is not None else None,
    )
    return Agent(
        model=model,
        system_prompt=TITLE_SYSTEM_INSTRUCTION,
    )


def build_title_agent(model_name: str, *, target: ModelTarget | None = None) -> Agent:  # noqa: ANN401
    """装配会话标题生成 Agent：另一个 system prompt，且不挂工具。"""
    return _build_title_agent_cached(model_name, _settings_cache_key(), target)


def reset_agent_cache() -> None:
    """清空装配缓存。供测试与「改了配置想立刻生效」的场景使用。"""
    _build_agent_cached.cache_clear()
    _build_title_agent_cached.cache_clear()

"""内置厂商预设：添加提供商时按厂商填 key 即可，不必手抄 Base URL 与鉴权头。

## 为什么需要预设

`ModelProvider` 里的 `base_url` 是「一个可独立寻址的 OpenAI 兼容端点」，这对自建
vLLM / LiteLLM 是对的——它们的地址本来就不固定。但对 OpenAI、Anthropic 这类厂商，
地址、认证头（Anthropic 用 `x-api-key` 还要带 `anthropic-version`）、兼容层路径
（Gemini 藏在 `/v1beta/openai` 下）都是公开且稳定的常识，让管理员手抄只会制造
「少一个 /v1 → 404 → 以为服务挂了」这类故障。

## 预设只是「创建时的默认值」，不是运行时依赖

`auth_style` / `extra_headers` 在创建那一刻从预设**抄进库**，之后运行时只读库。
预设目录随代码更新（厂商改地址、加版本头）不会影响已存在的提供商——那是管理员
审过、连通过的配置；要跟进新目录，删掉重建一次即可。

## 目录条目的取舍

只收**对话也走 OpenAI 兼容协议**的厂商：`ModelTarget` 用 `AsyncOpenAI` 打
`{base_url}/chat/completions`，原生协议不兼容的端点（如阿里百炼原生、火山方舟管控面）
收进来只会造成「列表能同步、对话必失败」。每个条目的 `note` 说明该厂商模型列表
能同步到哪些元数据——OpenAI 系的 `/v1/models` 大多只有 id，上下文窗口要靠
DeepSeek / Groq / OpenRouter 这类真正返回元数据的厂商。

`needs_api_key=False` 的本机预设（Ollama、LM Studio）是环回地址：出站守卫默认放行
私网，开了严格模式（`provider_block_private_targets`）后创建时就会被拒——那是配置
选择，不是预设的 bug。
"""

from dataclasses import dataclass, field

#: 认证方式：API Key 放进哪个请求头。与 `ModelProvider.auth_style` 对应。
AUTH_STYLES = frozenset({"bearer", "x-api-key", "x-goog-api-key", "none"})


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """一个可一键创建的厂商。

    `api_key_url` 是控制台地址——预设解决不了「去哪拿 key」，把它写进提示比让
    管理员去搜索引擎问强。`group` 用于后台选择界面的分组展示
    （overseas=海外厂商 / cn=国内厂商 / local=本机网关）。
    """

    key: str
    name: str
    base_url: str
    api_key_url: str
    auth_style: str = "bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    needs_api_key: bool = True
    note: str = ""
    group: str = "overseas"


_CATALOG: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        key="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_url="https://platform.openai.com/api-keys",
        note="模型列表只含 id 与归属，无上下文窗口等元数据。",
    ),
    ProviderPreset(
        key="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key_url="https://console.anthropic.com/settings/keys",
        extra_headers={"anthropic-version": "2023-06-01"},
        note="列表带展示名、上下文窗口与最大输出；官方接口分页，同步会自动翻页。",
    ),
    ProviderPreset(
        key="gemini",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_url="https://aistudio.google.com/apikey",
        note="经 Gemini 的 OpenAI 兼容层对接；列表只含 id，无元数据。",
    ),
    ProviderPreset(
        key="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_url="https://platform.deepseek.com/api_keys",
        note="列表带上下文窗口与最大输出。",
        group="cn",
    ),
    ProviderPreset(
        key="moonshot",
        name="月之暗面 Kimi",
        base_url="https://api.moonshot.cn/v1",
        api_key_url="https://platform.moonshot.cn/console/api-keys",
        note="列表带上下文窗口。",
        group="cn",
    ),
    ProviderPreset(
        key="dashscope",
        name="阿里云百炼（Qwen）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_url="https://bailian.console.aliyun.com/?apiKey=1",
        note="经兼容模式对接；列表只含 id。",
        group="cn",
    ),
    ProviderPreset(
        key="zhipu",
        name="智谱 BigModel",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_url="https://open.bigmodel.cn/usercenter/apikeys",
        note="列表只含 id。",
        group="cn",
    ),
    ProviderPreset(
        key="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_url="https://openrouter.ai/settings/keys",
        note="元数据最全：展示名、描述、上下文窗口、最大输出、思考能力都有。",
    ),
    ProviderPreset(
        key="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_url="https://console.groq.com/keys",
        note="列表带上下文窗口与最大输出。",
    ),
    ProviderPreset(
        key="mistral",
        name="Mistral",
        base_url="https://api.mistral.ai/v1",
        api_key_url="https://console.mistral.ai/api-keys",
        note="列表带展示名、描述与上下文窗口。",
    ),
    ProviderPreset(
        key="together",
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        api_key_url="https://api.together.ai/settings/api-keys",
        note="列表带展示名与上下文窗口；响应是裸数组，解析层已兼容。",
    ),
    ProviderPreset(
        key="fireworks",
        name="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_url="https://fireworks.ai/account/api-keys",
        note="列表只含 id。",
    ),
    ProviderPreset(
        key="xai",
        name="xAI (Grok)",
        base_url="https://api.x.ai/v1",
        api_key_url="https://console.x.ai",
        note="列表带上下文窗口。",
    ),
    ProviderPreset(
        key="siliconflow",
        name="SiliconFlow 硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        api_key_url="https://cloud.siliconflow.cn/account/ak",
        note="列表只含 id。",
        group="cn",
    ),
    ProviderPreset(
        key="ark",
        name="火山引擎方舟",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        note="列表带上下文窗口与最大输出。",
        group="cn",
    ),
    ProviderPreset(
        key="ollama",
        name="Ollama（本机）",
        base_url="http://localhost:11434/v1",
        api_key_url="https://ollama.com",
        needs_api_key=False,
        auth_style="none",
        note="免鉴权；开启严格内网拦截（provider_block_private_targets）后无法创建。",
        group="local",
    ),
    ProviderPreset(
        key="lmstudio",
        name="LM Studio（本机）",
        base_url="http://localhost:1234/v1",
        api_key_url="https://lmstudio.ai",
        needs_api_key=False,
        auth_style="none",
        note="免鉴权；开启严格内网拦截后无法创建。",
        group="local",
    ),
)

_PRESETS: dict[str, ProviderPreset] = {preset.key: preset for preset in _CATALOG}


def list_presets() -> list[ProviderPreset]:
    """全部内置预设，按目录顺序。"""
    return list(_CATALOG)


def get_preset(key: str) -> ProviderPreset | None:
    """按 key 取预设；未知 key 返回 None（由调用方决定报什么错）。"""
    return _PRESETS.get(key)

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

# =====================================================================
# 输入边界
#
# 这些上限是成本与稳定性边界，不是产品限制。所有 /api 端点都要求登录
# （`app/api/chat.py` 的 `current_active_user` 依赖），但登录用户同样能上传
# 任意长的历史：没有上限时单个请求就能把大量内容塞给上游模型。前端自行
# slice(-30)，但后端不能依赖客户端自律。
#
# 逐字段上限约束不了总量（100 条 × 32k 字符），因此 ChatRequest 另有一道总预算。
# 若某次部署需要不同数值，应把这一组常量提升为 settings 字段。
# =====================================================================
MAX_MESSAGE_CHARS = 32_000
MAX_THINKING_CHARS = 64_000
MAX_HISTORY_MESSAGES = 100
MAX_HISTORY_CHARS = 400_000
#: 对外模型 id 是 `{provider.slug}/{model_id}`（slug 最长 48，上游 id 最长 200），
#: 因此客户端字段上限要留出前缀空间，不能沿用旧的 200。
MAX_MODEL_ID_CHARS = 300
MAX_REASONING_EFFORT_CHARS = 32
MAX_TITLE_PROMPT_CHARS = 8_000

#: `reasoning_effort` 的字符白名单。刻意不枚举取值：网关可以声明 xhigh/max 之类
#: 的自定义等级（见 `model_service` 的 supported_reasoning_levels），枚举会误杀
#: 新等级。这里只挡住空白、空格、换行等不该出现在上游参数里的字符。
#: 长度写进模式串而不是只靠 max_length：空串同样不该发上游。数字与
#: MAX_REASONING_EFFORT_CHARS 由 f-string 绑定，避免两处漂移。
REASONING_EFFORT_PATTERN = rf"^[A-Za-z0-9._-]{{1,{MAX_REASONING_EFFORT_CHARS}}}$"


def _strip_optional_model(value: object) -> object:
    """`model` 的通用归一化：去首尾空白，全空白视为「未指定」。

    两个请求模型（对话与标题生成）共用这一份实现：留空表示「用后台默认模型」，而
    空白串与缺省必须落成同一个 None，否则 `model="   "` 会带着三个空格去查白名单，
    用户看到的是「模型   不在可用模型列表中」。
    """
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


def _coerce_message_content(value: object) -> object:
    """把「内容分片数组」收成纯文本，非文本分片直接拒绝。

    前端库（assistant-ui）用 `content: [{type: "text", text: "..."}]` 这种形状，而本接口
    的契约一直是 `content: str`。此前客户端按分片形状发过来只会拿到一条 Pydantic 的
    「Input should be a valid string」——既没说明**是哪个字段**、也没说明**为什么**。

    这里做两件事：

    1. 纯文本分片数组 → 拼成一个字符串（对调用方宽容，不必为一个格式细节重写客户端）；
    2. 含非文本分片（`image` / `file` …）→ 明确拒绝并点名类型。附件目前**不支持**，
       静默丢掉那个分片最糟：用户以为图片传上去了，模型却只看到一句文字。

    长度校验仍交给字段上的 `max_length`（拼接后的总长才是真正的边界）。
    """
    if not isinstance(value, (list, tuple)):
        return value

    texts: list[str] = []
    for part in value:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in (None, "text"):
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
            continue
        raise PydanticCustomError(
            "unsupported_content_part",
            "本条消息包含 {kind} 类型的内容；后端目前只接受纯文本，附件与多模态暂不支持",
            {"kind": str(kind)},
        )
    return "\n".join(texts)


class ChatMessage(BaseModel):
    # 只允许 user/assistant。此前还接受 "system"，而 convert_chat_history 会把它
    # 转成 SystemPromptPart 插进历史——那等于让调用方注入系统提示词，而系统提示词
    # 是服务端通过 prompt_manager 拥有的。该分支也从客户端不可达。
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=MAX_MESSAGE_CHARS)
    thinking: str | None = Field(None, max_length=MAX_THINKING_CHARS)

    _coerce_content = field_validator("content", mode="before")(_coerce_message_content)

    @model_validator(mode="after")
    def _require_user_content(self) -> "ChatMessage":
        """user 消息必须有内容，assistant 允许空。

        空白的 user 消息没有任何语义，只会让上游收到一次空 prompt（此前它一路走到
        服务层才被拒，客户端拿到的是 200 + SSE error）。assistant 侧必须放行空串：
        流式占位、以及「只产出 thinking 没有正文」的历史都会存成空 content。
        """
        if self.role == "user" and not self.content.strip():
            raise ValueError("user 消息内容不能为空白")
        return self


class ContextAnchor(BaseModel):
    """客户端回传的「上一轮真实用量」锚点。

    服务端不保存会话状态（历史由客户端全量回放），所以「这份历史到底占多少 token」
    只有上游知道——它上一轮回报过。把这个数字连同它对应的消息位置一起回传，预算就
    只需估算锚点**之后**的那几条新消息，而不是从零估算整份历史。

    为什么不能只要 `context_tokens` 不要位置：token 数与「截止到哪一条」是一体的，
    单给一个数字无从知道要再从哪条开始累加。
    """

    message_index: int = Field(
        ...,
        ge=0,
        description="锚点消息在 `messages` 里的下标；必须是 assistant（那一轮的用量是它产生的）",
    )
    context_tokens: int = Field(
        ...,
        gt=0,
        description="那一轮上游回报的上下文 token 数（最后一次请求的 prompt token 数）",
    )


class ChatRequest(BaseModel):
    model: str | None = Field(None, max_length=MAX_MODEL_ID_CHARS, description="选用的模型标识符")
    reasoning_effort: str | None = Field(
        None,
        max_length=MAX_REASONING_EFFORT_CHARS,
        pattern=REASONING_EFFORT_PATTERN,
        description="思考等级，例如 off/none/low/medium/high/xhigh/max",
    )
    messages: list[ChatMessage] = Field(
        ...,
        min_length=1,
        max_length=MAX_HISTORY_MESSAGES,
        description="历史与当前对话消息列表",
    )
    context_anchor: ContextAnchor | None = Field(
        None,
        description=(
            "可选：上一轮上游回报的真实用量锚点。提供后上下文预检只估算锚点之后的消息，"
            "精度高于纯估算；缺失或不可用时服务端自己估算（不报错，因为这是可选的精度提升）。"
        ),
    )

    _strip_model = field_validator("model", mode="before")(_strip_optional_model)

    @model_validator(mode="after")
    def _check_message_order(self) -> "ChatRequest":
        """历史必须以 user 开头、严格 user/assistant 交替、以 user 结尾。

        上游按对话结构解析：Anthropic Messages API 要求首条为 user，`[assistant,
        user]` 会直接 400；末条不是 user 则根本不是一次新的提问。此前这两类要走到
        服务层才被拒，客户端拿到的是 200 + SSE error——监控会把它记成成功。
        服务层保留等价检查作为防御（`convert_chat_history` 也可能被别处直接调用）。
        末条「非空白」由 ChatMessage 的校验保证。

        交替同理：`convert_chat_history` 把历史装配成 request/response 交替的序列，
        连续两条同角色会被合并或丢弃，模型看到的上下文与用户以为的不一致，而请求本身
        却「成功」了。判定顺序按「首条 → 交替 → 末条」：先定位到最早出错的位置，用户
        照着第一条报错改完再看下一条。

        注意这里刻意不拦「末条是 assistant」以外的连续同角色组合——交替校验已经覆盖
        了它们，两处各写一份只会有两份文案不一致的风险。
        """
        first = self.messages[0]
        if first.role != "user":
            raise ValueError("第一条消息必须是 user 消息")

        for index in range(1, len(self.messages)):
            previous, current = self.messages[index - 1], self.messages[index]
            if current.role == previous.role:
                raise ValueError(
                    f"消息必须以 user 与 assistant 交替出现：第 {index} 条与第 {index + 1} 条都是 {current.role} 消息"
                )

        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须是 user 消息")
        return self

    @model_validator(mode="after")
    def _check_history_budget(self) -> "ChatRequest":
        """约束历史总长度。

        没有这一道，max_length 只是把「无限」变成「很大」：100 条各 32k 字符的
        消息仍然能通过逐字段校验。
        """
        total = sum(len(m.content) + len(m.thinking or "") for m in self.messages)
        if total > MAX_HISTORY_CHARS:
            raise ValueError(f"对话历史总长度 {total} 字符超出上限 {MAX_HISTORY_CHARS} 字符，请精简输入或开始新会话")
        return self


class ModelItem(BaseModel):
    id: str
    name: str
    provider: str
    #: 上游真实模型 ID（`id` 去掉 `{provider.slug}/` 前缀的部分）。
    #:
    #: 展示层需要它：同一个提供商下可以有多个路由前缀指向同名模型
    #: （`auto/deepseek`、`workbuddy/deepseek` 的 display_name 都是 Deepseek），
    #: 只给 `provider` + `name` 的话聊天框里会出现几条一模一样的条目，看不出区别。
    #: 旧版网关路径（模型 id 本身就是完整上游 id）留空，前端回退到只显示 provider。
    model_id: str | None = None
    description: str | None = None
    # 三态：True/False 来自网关的明确表态，None 表示网关没给这个字段（前端按「未知」处理）。
    reasoning: bool | None = False
    supported_reasoning_levels: list[str] = Field(default_factory=list)
    context_window: int | None = None
    max_output_tokens: int | None = None
    is_default: bool = False


class ModelListResponse(BaseModel):
    models: list[ModelItem]
    default_model: str


class TitleGenerateRequest(BaseModel):
    model: str | None = Field(None, max_length=MAX_MODEL_ID_CHARS, description="使用的模型名称")
    prompt: str = Field(..., min_length=1, max_length=MAX_TITLE_PROMPT_CHARS, description="首条提示词内容")

    _strip_model = field_validator("model", mode="before")(_strip_optional_model)

    @field_validator("prompt")
    @classmethod
    def _reject_blank_prompt(cls, v: str) -> str:
        """纯空白的 prompt 与空串等价，同样不该白打一次上游模型。"""
        if not v.strip():
            raise ValueError("prompt 不能为空白")
        return v


class TitleGenerateResponse(BaseModel):
    title: str

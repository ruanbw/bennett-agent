from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

# =====================================================================
# 输入边界：成本与稳定性边界而非产品限制；逐字段上限约束不了总量，故 ChatRequest 另有总预算。
MAX_MESSAGE_CHARS = 32_000
MAX_THINKING_CHARS = 64_000
MAX_HISTORY_MESSAGES = 100
MAX_HISTORY_CHARS = 400_000
#: 路由键是 `{slug}/{model_id}`（slug ≤48、id ≤200），字段上限要留出前缀空间。
MAX_MODEL_ID_CHARS = 300
MAX_REASONING_EFFORT_CHARS = 32
MAX_TITLE_PROMPT_CHARS = 8_000

#: 客户端生成的 `sess_<...>` 标识（见 `frontend/src/lib/constants.ts` 的 `newId`）。
#: 字符集必须与 `app/schemas/sync.py:ID_PATTERN` 一致，否则同步接受的 id 聊天会 422。
MAX_SESSION_ID_CHARS = 64
SESSION_ID_PATTERN = rf"^sess_[A-Za-z0-9_-]{{1,{MAX_SESSION_ID_CHARS - 5}}}$"

#: `reasoning_effort` 白名单：不枚举取值（网关可声明自定义等级，枚举会误杀），只挡空白字符。
#: 长度写进模式串而非只靠 max_length，空串同样不该发上游。
REASONING_EFFORT_PATTERN = rf"^[A-Za-z0-9._-]{{1,{MAX_REASONING_EFFORT_CHARS}}}$"


def _strip_optional_model(value: object) -> object:
    """`model` 的归一化：去首尾空白，全空白视为「未指定」。

    空白串与缺省必须落成同一个 None，否则 `model="   "` 会带着空格去查白名单。
    """
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


def _coerce_message_content(value: object) -> object:
    """把前端库的分片数组收成纯文本，非文本分片直接拒绝。

    静默丢掉 `image`/`file` 分片最糟：用户以为图片传上去了，模型只看到一句文字。
    长度校验仍交给字段的 `max_length`（拼接后的总长才是边界）。
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


#: 单轮回放的助手工具调用上限：这些由客户端回放，没上限就能塞几千条放大请求体。
MAX_REPLAYED_TOOL_CALLS = 50

#: 工具调用入参的字符上限。不能压得太低：审批续跑时回放的 args 超限就 422，而用户改不了
#: 模型生成的入参。总量已由 `MAX_REQUEST_BODY_BYTES` 兜住，这里取 64k。
MAX_TOOL_ARGS_CHARS = 64_000

#: 单个工具调用 id 的字符上限。上游生成的 id 通常几十字符，长的是 base64 变体。
MAX_TOOL_CALL_ID_CHARS = 200


class ReplayedToolCall(BaseModel):
    """客户端回放的一次工具调用：审批是无状态续跑，第二次请求只能从历史里知道要调什么。

    不收 `outcome`/结果文本：工具结果由服务端产生，回放等于允许伪造「删除已成功」。
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=MAX_TOOL_CALL_ID_CHARS)
    tool_name: str = Field(min_length=1, max_length=128)
    #: 调用入参，JSON 文本：保持字符串以便解析失败时原样拒绝，而不是猜。
    args: str = Field(default="{}", max_length=MAX_TOOL_ARGS_CHARS)


class ChatMessage(BaseModel):
    # 只允许 user/assistant：system 会被当成 SystemPromptPart 插进历史，等于让调用方注入系统提示词。
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=MAX_MESSAGE_CHARS)
    thinking: str | None = Field(None, max_length=MAX_THINKING_CHARS)
    #: 这条助手消息里发起过的工具调用（只用于审批续跑，见 `ReplayedToolCall`）；
    #: 只有 assistant 允许带，user 带了等于伪造模型行为。
    tool_calls: list[ReplayedToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_tool_calls(self) -> "ChatMessage":
        """工具调用只能挂在 assistant 消息上。

        不校验的话 user 消息会被合成一条 `ModelResponse`，凭空给模型塞一句它没说过的话。
        """
        if self.tool_calls and self.role != "assistant":
            raise ValueError("只有 assistant 消息可以携带 tool_calls")
        if len(self.tool_calls) > MAX_REPLAYED_TOOL_CALLS:
            raise ValueError(f"单条消息的工具调用不能超过 {MAX_REPLAYED_TOOL_CALLS} 个")
        return self

    _coerce_content = field_validator("content", mode="before")(_coerce_message_content)

    @model_validator(mode="after")
    def _require_user_content(self) -> "ChatMessage":
        """user 消息必须有内容，assistant 允许空。

        空白 user 只会让上游收到一次空 prompt；assistant 放行空串，因为只产出 thinking 的历史就是空 content。
        """
        if self.role == "user" and not self.content.strip():
            raise ValueError("user 消息内容不能为空白")
        return self


class ContextAnchor(BaseModel):
    """客户端回传的上一轮真实用量锚点：有了它只需估算锚点之后的新消息。

    token 数必须连同「截止到哪一条」一起给，单给数字无从知道从哪条开始累加。
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
    #: 当前会话的客户端标识；它不是身份，作用域由服务端注入（`app/tools/sandbox_scope.py`），
    #: 租约查询一律带 `user_id`，谎报别人的 id 只会拿到自己名下的租约。
    session_id: str | None = Field(
        None,
        max_length=MAX_SESSION_ID_CHARS,
        pattern=SESSION_ID_PATTERN,
        description="客户端生成的会话标识（sess_<...>）；沙箱按会话分配机器时使用",
    )
    #: 审批决定：`tool_call_id` → 是否批准，只用于续跑被拦下的工具调用。
    #: 缺失的一律按拒绝处理（见 `app/tools/approval.py`）：关掉页面不能等于同意删除。
    approvals: dict[str, bool] = Field(
        default_factory=dict,
        description="待审批工具调用的决定（tool_call_id → 是否批准）；缺失的一律按拒绝处理",
    )

    _strip_model = field_validator("model", mode="before")(_strip_optional_model)

    @model_validator(mode="after")
    def _check_approvals(self) -> "ChatRequest":
        """审批决定的形状校验：数量有上限，且每个 id 都能在历史里找到对应工具调用。

        不在历史里的 id 会让 pydantic-ai 抛内部错误，这里提前拒绝并点名。
        """
        if len(self.approvals) > MAX_REPLAYED_TOOL_CALLS:
            raise ValueError(f"单次请求的审批决定不能超过 {MAX_REPLAYED_TOOL_CALLS} 条")

        if not self.approvals:
            return self

        known: set[str] = set()
        for message in self.messages:
            for call in message.tool_calls:
                known.add(call.tool_call_id)

        unknown = [call_id for call_id in self.approvals if call_id not in known]
        if unknown:
            raise ValueError(
                f"审批决定里的工具调用 {unknown[0]} 不在本次对话历史中；只有历史里出现过的工具调用才能被审批"
            )
        return self

    @model_validator(mode="after")
    def _check_message_order(self) -> "ChatRequest":
        """历史必须以 user 开头、user/assistant 严格交替、以 user 结尾。

        Anthropic 要求首条为 user，`[assistant, user]` 直接 400；连续同角色会被合并或丢弃，模型看到的上下文就变了。
        判定顺序按首条 → 交替 → 末条，先报最早出错的位置；服务层保留等价检查作防御。
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

        # 续跑审批时允许以 assistant 结尾：那一条带着待审批的工具调用，点「批准」不算新提问。
        if self.messages[-1].role != "user":
            last = self.messages[-1]
            if not (self.pending_approval_ids and last.tool_calls):
                raise ValueError("最后一条消息必须是 user 消息")
        return self

    @property
    def pending_approval_ids(self) -> set[str]:
        """本次请求真正要续跑的调用 id：`approvals` 经形状校验后的子集。"""
        if not self.approvals:
            return set()
        known = {call.tool_call_id for message in self.messages for call in message.tool_calls}
        return {call_id for call_id in self.approvals if call_id in known}

    @property
    def is_resuming_approval(self) -> bool:
        """这次请求是不是续跑一次被拦下的工具调用。

        只看 `approvals` 非空不够：客户端可能顺手带上旧审批记录，那种应按普通请求处理。
        """
        return bool(self.pending_approval_ids)

    @model_validator(mode="after")
    def _check_history_budget(self) -> "ChatRequest":
        """约束历史总长度：逐字段 max_length 只是把「无限」变成「很大」。"""
        total = sum(len(m.content) + len(m.thinking or "") for m in self.messages)
        if total > MAX_HISTORY_CHARS:
            raise ValueError(f"对话历史总长度 {total} 字符超出上限 {MAX_HISTORY_CHARS} 字符，请精简输入或开始新会话")
        return self


class ModelItem(BaseModel):
    id: str
    name: str
    provider: str
    #: 上游真实模型 ID（`id` 去掉 `{provider.slug}/` 前缀）；同一提供商下多个前缀可能指向
    #: 同名模型，只给 `provider` + `name` 会出现几条一模一样的条目。网关路径留空。
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

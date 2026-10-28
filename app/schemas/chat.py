from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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

#: 会话标识的长度与形状。它是**客户端生成**的 `sess_<...>`（见 `frontend/src/lib/constants.ts`
#: 的 `newId`）：安全上下文里是去掉短横的 uuid，非安全上下文（局域网 http）回退成
#: `${时间戳}_${随机串}`——**旧格式带下划线**。
#:
#: 字符集必须与 `app/schemas/sync.py:ID_PATTERN` 一致（`[A-Za-z0-9_-]`）：同步接受、
#: 聊天拒绝的 id 会让老会话的每一次对话都 422，而这不是「沙箱不可用」，是**聊天不可用**。
#: 以后收窄这个 pattern 之前先想清楚：存量数据的 id 不会因为 schema 变严而消失。
MAX_SESSION_ID_CHARS = 64
SESSION_ID_PATTERN = rf"^sess_[A-Za-z0-9_-]{{1,{MAX_SESSION_ID_CHARS - 5}}}$"

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


#: 单轮里回放的助手工具调用上限。
#:
#: 一个正常回答很少超十次调用；给上限是因为它们由客户端回放，没有上限意味着一个
#: 篡改过的客户端可以塞进几千条来放大请求体。超限直接 422（见 `_check_tool_calls`）。
MAX_REPLAYED_TOOL_CALLS = 50

#: 工具调用入参的字符上限。入参是模型生成的 JSON，正常远小于这个数；这里防的是
#: 客户端回放一份超大入参来撑爆上游请求体。
#:
#: ## 8000 是个 bug，不是设计
#:
#: 上限的初衷（「防客户端塞超大入参」）是对的，但它同时限制了**模型真实产出的合法
#: 入参**：`sandbox_write_file` 写一个大脚本、MCP 的 `write_file`、带长 SQL/JSON 的
#: 写工具，入参都可能超过 8000。后果不是「少一次工具调用」，而是**这条审批路径彻底
#: 走不通**——审批卡片正常弹出，用户点「批准」，回放的 args 超长 → 422，而用户改不了
#: 模型生成的入参、也没有别的出口。
#:
#: 真正该兜住总量的是 `MAX_REQUEST_BODY_BYTES`（32MB）与 `MAX_HISTORY_CHARS`，
#: 前者已经挡住了「撑爆请求体」。这里放宽到 64k：仍然远小于整个请求体上限（50 个
#: 调用 × 64k ≈ 3.2MB），但足够覆盖任何真实的工具入参。
MAX_TOOL_ARGS_CHARS = 64_000

#: 单个工具调用 id 的字符上限。上游生成的 id 通常几十字符，长的是 base64 变体。
MAX_TOOL_CALL_ID_CHARS = 200


class ReplayedToolCall(BaseModel):
    """客户端回放的**一次工具调用**（用于审批续跑）。

    ## 为什么需要它

    审批是**无状态续跑**的：工具在执行前被拦下 → 本轮结束 → 用户点批准 → 下一次
    请求。服务端不保存会话，所以第二次请求必须自己知道「上一次到底要调用什么」。
    这些信息只能来自客户端回放的历史。

    ## 为什么只允许这几个字段

    只有 `tool_call_id` / `tool_name` / `args` 三样对续跑有用。**不收 `outcome` 或
    结果文本**：工具结果由服务端产生，让客户端回放等于允许伪造「这个删除已经成功
    了」。续跑时 pydantic-ai 自己会用 `DeferredToolResults` 补结果，不需要历史里有。
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=MAX_TOOL_CALL_ID_CHARS)
    tool_name: str = Field(min_length=1, max_length=128)
    #: 调用入参，**JSON 文本**。保持字符串而不是 dict：它是模型生成的原始产物，
    #: 解析失败时应当原样拒绝（写错的 JSON 说明客户端在伪造或损坏），而不是猜。
    args: str = Field(default="{}", max_length=MAX_TOOL_ARGS_CHARS)


class ChatMessage(BaseModel):
    # 只允许 user/assistant。此前还接受 "system"，而 convert_chat_history 会把它
    # 转成 SystemPromptPart 插进历史——那等于让调用方注入系统提示词，而系统提示词
    # 是服务端通过 prompt_manager 拥有的。该分支也从客户端不可达。
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=MAX_MESSAGE_CHARS)
    thinking: str | None = Field(None, max_length=MAX_THINKING_CHARS)
    #: 这条助手消息里发起过的工具调用（只用于审批续跑，见 `ReplayedToolCall`）。
    #:
    #: 只有 assistant 消息允许带；user 消息带了等于伪造模型行为，直接拒绝。
    tool_calls: list[ReplayedToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_tool_calls(self) -> "ChatMessage":
        """工具调用只能挂在 assistant 消息上，且数量有上限。

        不校验的话，一个带 `tool_calls` 的 user 消息会进到重建逻辑里，被合成一条
        `ModelResponse`——那是一条**凭空出现的助手回复**，模型会以为自己在上一轮
        说过这些话。
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
    #: 当前会话的客户端标识。
    #:
    #: 为什么服务端需要它：隔离执行环境是「每会话一台机器」，所以服务端必须知道
    #: 「这是哪个会话」才能找到对应租约。它**不是身份**——身份始终来自登录态，
    #: 作用域由服务端注入（`app/tools/sandbox_scope.py`）。即便客户端谎报别人的
    #: 会话 id，拿到的也只是自己名下的租约：租约查询一律带 `user_id`。
    #:
    #: 可选：历史客户端不传时，沙箱工具会回「当前会话缺少会话标识」，其余能力不受影响。
    session_id: str | None = Field(
        None,
        max_length=MAX_SESSION_ID_CHARS,
        pattern=SESSION_ID_PATTERN,
        description="客户端生成的会话标识（sess_<...>）；沙箱按会话分配机器时使用",
    )
    #: 审批决定：`tool_call_id` → 是否批准。**只用于续跑**被拦下的工具调用。
    #:
    #: 未出现在这里的待审批调用一律按**拒绝**处理（见 `app/tools/approval.py`）：
    #: 「没有回应」绝不能等于「批准」，否则用户关掉页面就等于同意了一次删除。
    approvals: dict[str, bool] = Field(
        default_factory=dict,
        description="待审批工具调用的决定（tool_call_id → 是否批准）；缺失的一律按拒绝处理",
    )

    _strip_model = field_validator("model", mode="before")(_strip_optional_model)

    @model_validator(mode="after")
    def _check_approvals(self) -> "ChatRequest":
        """审批决定的形状校验。

        两条：
        1. 数量有上限（防篡改客户端塞几千条）；
        2. **每个 id 必须能在历史里找到对应的工具调用**。不在历史里的 id 无法续跑
           任何东西——pydantic-ai 拿不到对应的 `ToolCallPart`，会抛出一个用户完全
           看不懂的内部错误。这里提前拒绝，文案说得清是哪个 id。
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

        # 续跑审批时**允许**以 assistant 结尾：那一条是带着待审批工具调用的助手消息，
        # 用户点「批准」并没有产生新的提问。除此之外仍必须以 user 结尾——末条不是
        # user 就不是一次新的提问。
        if self.messages[-1].role != "user":
            last = self.messages[-1]
            if not (self.pending_approval_ids and last.tool_calls):
                raise ValueError("最后一条消息必须是 user 消息")
        return self

    @property
    def pending_approval_ids(self) -> set[str]:
        """本次请求真正要续跑的调用 id（在历史里能找到对应工具调用的那些）。

        与 `approvals` 的区别：`approvals` 是客户端给的原样决定，`pending_approval_ids`
        是**经过形状校验**的子集。其余字段与校验都用它，以免两处对「什么算待审批」
        给出不同答案。
        """
        if not self.approvals:
            return set()
        known = {call.tool_call_id for message in self.messages for call in message.tool_calls}
        return {call_id for call_id in self.approvals if call_id in known}

    @property
    def is_resuming_approval(self) -> bool:
        """这次请求是不是「续跑一次被拦下的工具调用」。

        判定看两头：有审批决定，且历史里有对应的工具调用。只看 `approvals` 非空
        不够——客户端可能在一次普通请求里顺手带上了旧的审批记录（比如重试了上一次
        请求），那时应当按普通请求处理，而不是去续跑一个本轮根本不存在的调用。
        """
        return bool(self.pending_approval_ids)

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

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# =====================================================================
# 输入边界
#
# 这些上限是成本与稳定性边界，不是产品限制。/api/chat 允许匿名调用且无鉴权，
# 没有上限时单个请求就能把任意长的历史塞给上游模型。前端自行 slice(-30)，
# 但后端不能依赖客户端自律。
#
# 逐字段上限约束不了总量（100 条 × 32k 字符），因此 ChatRequest 另有一道总预算。
# 若某次部署需要不同数值，应把这一组常量提升为 settings 字段。
# =====================================================================
MAX_MESSAGE_CHARS = 32_000
MAX_THINKING_CHARS = 64_000
MAX_HISTORY_MESSAGES = 100
MAX_HISTORY_CHARS = 400_000
MAX_MODEL_ID_CHARS = 200
MAX_REASONING_EFFORT_CHARS = 32
MAX_TITLE_PROMPT_CHARS = 8_000


class ChatMessage(BaseModel):
    # 只允许 user/assistant。此前还接受 "system"，而 convert_chat_history 会把它
    # 转成 SystemPromptPart 插进历史——那等于让调用方注入系统提示词，而系统提示词
    # 是服务端通过 prompt_manager 拥有的。该分支也从客户端不可达。
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=MAX_MESSAGE_CHARS)
    thinking: str | None = Field(None, max_length=MAX_THINKING_CHARS)


class ChatRequest(BaseModel):
    model: str | None = Field(None, max_length=MAX_MODEL_ID_CHARS, description="选用的模型标识符")
    reasoning_effort: str | None = Field(
        None,
        max_length=MAX_REASONING_EFFORT_CHARS,
        description="思考等级，例如 off/none/low/medium/high/xhigh/max",
    )
    messages: list[ChatMessage] = Field(
        ...,
        min_length=1,
        max_length=MAX_HISTORY_MESSAGES,
        description="历史与当前对话消息列表",
    )
    stream: bool = Field(True, description="是否启用流式返回")

    @model_validator(mode="after")
    def _check_history_budget(self) -> "ChatRequest":
        """约束历史总长度。

        没有这一道，max_length 只是把「无限」变成「很大」：100 条各 32k 字符的
        消息仍然能通过逐字段校验。
        """
        total = sum(len(m.content) + len(m.thinking or "") for m in self.messages)
        if total > MAX_HISTORY_CHARS:
            raise ValueError(
                f"对话历史总长度 {total} 字符超出上限 {MAX_HISTORY_CHARS} 字符，"
                "请精简输入或开始新会话"
            )
        return self


class ModelItem(BaseModel):
    id: str
    name: str
    provider: str
    description: str | None = None
    reasoning: bool = False
    supported_reasoning_levels: list[str] = Field(default_factory=list)
    context_window: int | None = None
    max_output_tokens: int | None = None
    is_default: bool = False


class ModelListResponse(BaseModel):
    models: list[ModelItem]
    default_model: str


class TitleGenerateRequest(BaseModel):
    model: str | None = Field(None, max_length=MAX_MODEL_ID_CHARS, description="使用的模型名称")
    prompt: str = Field(..., max_length=MAX_TITLE_PROMPT_CHARS, description="首条提示词内容")


class TitleGenerateResponse(BaseModel):
    title: str

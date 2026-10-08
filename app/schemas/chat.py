from typing import Any, List, Literal, Optional
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    thinking: Optional[str] = None


class ChatRequest(BaseModel):
    model: Optional[str] = Field(None, description="选用的模型标识符")
    reasoning_effort: Optional[str] = Field(None, description="思考等级，例如 off/none/low/medium/high/xhigh/max")
    messages: List[ChatMessage] = Field(..., min_length=1, description="历史与当前对话消息列表")
    stream: bool = Field(True, description="是否启用流式返回")


class ModelItem(BaseModel):
    id: str
    name: str
    provider: str
    description: Optional[str] = None
    reasoning: bool = False
    supported_reasoning_levels: List[str] = Field(default_factory=list)
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    is_default: bool = False


class ModelListResponse(BaseModel):
    models: List[ModelItem]
    default_model: str


class TitleGenerateRequest(BaseModel):
    model: Optional[str] = Field(None, description="使用的模型名称")
    prompt: str = Field(..., description="首条提示词内容")


class TitleGenerateResponse(BaseModel):
    title: str


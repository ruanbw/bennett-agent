"""本轮生成的**真实** token 用量：从上游响应里取，不估算。

上游每次响应都带 usage（OpenAI 兼容的 `prompt_tokens` / `completion_tokens`），
pydantic-ai 把它记在每条 `ModelResponse.usage` 上，并在 `run_stream_events` 的
**末尾**以一个 `AgentRunResultEvent` 交回运行结果——也就是说这份数据一直在本项目
消费的那条流里，只是此前被丢掉了（`translate_event` 还把它当成「未识别的
pydantic-ai 流事件」打了 WARNING）。真实值比字符数、比 `token_budget` 的估算都准，
所以只要拿得到就用它。

两个容易搞错的点，都在下面这个函数里定死：

1. **取最后一次请求的 `input_tokens`，不是整轮的累加。** 一轮里如果有工具调用，
   会有多次模型请求，每次都必须重发一遍历史，`RunUsage.input_tokens` 是这些请求的
   **总和**（例如 1066 + 1400 = 2466）。真正等于「当前上下文长度」的只有最后一次
   请求的 prompt 数。
2. **拿不到就不报。** 部分网关在流式模式下不回 usage（`input_tokens` 为 0），此时
   返回 `None` 由调用方跳过这个事件——报一个 0 会被读成「上下文是空的」，比不报更糟。
"""

import logging
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ModelResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnUsage:
    """一轮生成的真实用量。三个数字都来自上游，未做任何换算。"""

    #: 本轮最后一次请求的 prompt token 数 = 当前上下文占用的长度（含缓存命中部分）
    context_tokens: int
    #: 本轮所有请求的输出 token 之和（工具调用间隙生成的内容也算）
    output_tokens: int
    #: 上下文里命中上游提示缓存的部分（`context_tokens` 的子集）
    cache_read_tokens: int


def extract_turn_usage(messages: list[ModelMessage] | None) -> TurnUsage | None:
    """从一轮运行的消息序列里取真实用量；拿不到（或上游没报）返回 `None`。"""
    responses = [message for message in messages or [] if isinstance(message, ModelResponse)]
    if not responses:
        return None

    last_usage = responses[-1].usage
    context_tokens = int(getattr(last_usage, "input_tokens", 0) or 0)
    if context_tokens <= 0:
        # 上游没在流式响应里回 usage。宁可让这个事件缺席，也不要给一个 0。
        logger.debug("上游未返回 usage（最后一次请求的 input_tokens 为 0），本轮不报用量")
        return None

    return TurnUsage(
        context_tokens=context_tokens,
        output_tokens=sum(int(getattr(message.usage, "output_tokens", 0) or 0) for message in responses),
        cache_read_tokens=int(getattr(last_usage, "cache_read_tokens", 0) or 0),
    )

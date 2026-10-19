"""工具执行异常隔离。

pydantic-ai 的默认语义是：工具函数抛出非 `ModelRetry` 异常时，异常会中止整轮生成。
但工具是外部边界（网络、第三方 SDK、上游返回的形状），一个工具里的 bug 不该让用户
这一轮回答直接作废——尤其不该在正文已经流出、其它工具已经成功之后。

这里用 pydantic-ai 的 capability 钩子 `on_tool_execute_error` 把意外异常降级成一条
**工具结果**：模型看到「工具失败」后可以换工具、换参数或如实向用户说明，整轮继续。
异常原文只进日志（可能含上游 URL、密钥上下文），不回灌给模型。

## 哪些异常不归这里管

`ModelRetry` 是 **控制流**，不是错误：它携带给模型看的「你哪里做错了、该怎么改」
文案，由 pydantic-ai 自己的重试计数管理。把它也降级成固定文案会同时造成两件事：

1. 模型再也看不到真实原因（参数校验失败的具体字段、工具超时了多久）；
2. 收到一句「不要原样重复调用同一个工具」——对参数校验失败来说正好是反向指导，
   而正确做法恰恰是按提示改正参数再调一次。

pydantic-ai 的 `on_tool_execute_error` 在实现上**已经**把 `ModelRetry` 排除在外
（`tool_manager.py` 在 `except (ToolFailed, ModelRetry): raise` 分支就上抛了，不会走到
本钩子）。这里的显式早退是**第二道保险**：它是一个只依赖公开 API 的断言，不依赖
某个版本的内部 except 顺序。以后升级 pydantic-ai 时若那个分支被重排，我们有测试
会当场变红，而不是工具重试语义静默消失。

边界：`asyncio.CancelledError` 是 `BaseException`，不会进入本钩子，客户端断开仍按
取消语义处理（见 `app/services/chat_service.py`）。工具自己捕获并返回错误文案的路径
（如 `tavily_search`）也不受影响——这里只兜「漏网抛出来」的那一类。
"""

import logging
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

logger = logging.getLogger(__name__)

#: 回灌给模型的失败文案。刻意写成「结果」而不是抛错：模型据此调整策略，
#: 同时避免把内部异常原文（可能含 URL / 密钥上下文）放进上下文。
#: 「不要原样重复调用」是给模型的约束：同一 bug 再调一次大概率还是失败，
#: 重复调用只会白烧 token，并且让带副作用的工具执行两次。
TOOL_FAILURE_RESULT = (
    "工具执行失败（内部错误），本次没有取得结果。"
    "不要原样重复调用同一个工具；请换一种方式继续，或如实告知用户该步骤失败。"
)


class ToolErrorIsolation(AbstractCapability[None]):
    """把工具的意外异常转成「失败结果」，让本轮对话继续。"""

    async def on_tool_execute_error(
        self,
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,  # noqa: ANN401
        error: Exception,
    ) -> Any:  # noqa: ANN401
        # 控制流原样上抛（详见模块 docstring）：这是重试语义的第二道保险。
        if isinstance(error, ModelRetry):
            raise error

        # 参数可能是用户数据（搜索词、提问内容），只记工具名与异常。
        # traceback 必须留全，否则工具 bug 会从此变成一条「静默的失败结果」。
        logger.error(
            "工具执行失败 (tool=%s, tool_call_id=%s)",
            call.tool_name,
            call.tool_call_id,
            exc_info=error,
        )
        return TOOL_FAILURE_RESULT

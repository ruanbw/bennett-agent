"""工具审批（HITL）的行为锁。

这一层把 `RiskLevel` 从元数据变成真的执行闸门。它最重要的性质只有三条，每条都必须
钉死，因为任何一条错了都会表现为「用户点过批准，但工具没跑」或者更糟「没点批准，
工具跑了」：

1. **未批准绝不执行**：被拦下时工具一次都不该被调用；
2. **批准后只执行一次**：续跑不能重复执行，也不能因为「已批准」而跳过风险检查
   导致死循环；
3. **没有回应 = 拒绝**：缺失的决定一律按拒绝处理——否则用户关掉页面就等于同意了
   一次删除。

续跑靠 `_resume_agent` 这套假模型驱动：它模拟「模型先发起工具调用、看到结果后收尾」
的两轮行为，从而让真实的 pydantic-ai 审批管线跑起来（不联网）。
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from app.schemas.chat import ChatMessage
from app.services.chat_service import build_resume_results_for, convert_chat_history
from app.tools.approval import (
    APPROVAL_REQUIRED_LEVELS,
    DENIED_RESULT,
    ApprovalGate,
    all_deferred_ids,
    requires_approval,
)
from app.tools.registry import RiskLevel


# ---------------------------------------------------------------------
# 分级判定
# ---------------------------------------------------------------------
@pytest.mark.parametrize("level", [RiskLevel.SAFE_WRITE, RiskLevel.DANGEROUS])
def test_write_levels_require_approval(level):
    assert requires_approval(level) is True


@pytest.mark.parametrize("level", [RiskLevel.READ_ONLY, RiskLevel.INTERACTIVE])
def test_read_only_levels_run_without_asking(level):
    """只读没有副作用；INTERACTIVE 本身就是问用户，再审批一次是荒谬的。"""
    assert requires_approval(level) is False


def test_missing_level_does_not_require_approval():
    """没有分级信息的工具（例如旧注册路径）不拦：拦了就等于默认全部不可用。"""
    assert requires_approval(None) is False


def test_unknown_level_is_conservative():
    """未知等级按需要审批处理：漏审的代价比多问一次大得多。"""
    assert requires_approval("totally_new_level") is True


def test_string_levels_are_accepted():
    """MCP 工具的风险等级来自配置（字符串），不是枚举。"""
    assert requires_approval("dangerous") is True
    assert requires_approval("read_only") is False


def test_safe_write_is_in_the_gate():
    """分界线是「能不能撤销」：本项目的删除是硬删除，没有回收站。"""
    assert RiskLevel.SAFE_WRITE in APPROVAL_REQUIRED_LEVELS
    assert RiskLevel.DANGEROUS in APPROVAL_REQUIRED_LEVELS


# ---------------------------------------------------------------------
# 用假模型驱动真实的审批管线
# ---------------------------------------------------------------------
EXECUTED: list[dict[str, object]] = []


async def _write_tool(thing_id: int) -> str:
    """删除一条记录（不可撤销）。

    Args:
        thing_id: 记录 id
    """
    EXECUTED.append({"thing_id": thing_id})
    return f"deleted {thing_id}"


def _build_agent(risk: RiskLevel = RiskLevel.DANGEROUS) -> Agent[None, str | DeferredToolRequests]:
    """装配一个「第一轮调用工具、看到结果后收尾」的假模型 Agent。"""

    def model_fn(messages: list[ModelMessage], info: object) -> ModelResponse:  # noqa: ANN401
        """非流式版本：`run()` 与续跑都用它（不需要增量）。"""
        for message in messages:
            for part in message.parts:
                if getattr(part, "part_kind", "") == "tool-return":
                    return ModelResponse(parts=[TextPart(f"已完成：{part.content}")])
        return ModelResponse(parts=[ToolCallPart("write_tool", {"thing_id": 7}, tool_call_id="call-fixed")])

    def stream_fn(messages: list[ModelMessage], info: object) -> object:  # noqa: ANN401
        async def gen():
            for message in messages:
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "tool-return":
                        yield f"已完成：{part.content}"
                        return
            # 还没有结果：发起调用
            yield {
                0: DeltaToolCall(
                    name="write_tool",
                    json_args='{"thing_id": 7}',
                    tool_call_id="call-fixed",
                )
            }

        return gen()

    tool = Tool(
        _write_tool,
        name="write_tool",
        metadata={"risk_level": risk.value, "description": "删除一条记录"},
    )
    return Agent(
        FunctionModel(model_fn, stream_function=stream_fn),
        tools=[tool],
        capabilities=[ApprovalGate()],
        output_type=str | DeferredToolRequests,
    )


@pytest.fixture(autouse=True)
def _reset_executed():
    EXECUTED.clear()
    yield
    EXECUTED.clear()


async def test_dangerous_tool_is_not_executed_without_approval():
    """核心断言：被拦下时工具一次都没跑。

    这一条错了就是安全事故——用户看到一个审批框，实际上删除已经发生了。
    """
    agent = _build_agent(RiskLevel.DANGEROUS)

    result = await agent.run("删掉 7")

    assert isinstance(result.output, DeferredToolRequests)
    assert EXECUTED == []
    assert [call.tool_call_id for call in result.output.approvals] == ["call-fixed"]


async def test_read_only_tool_runs_without_approval():
    """只读工具不被拦：多问一次只会让用户觉得系统啰嗦。"""
    agent = _build_agent(RiskLevel.READ_ONLY)

    result = await agent.run("删掉 7")

    assert isinstance(result.output, str)
    assert EXECUTED == [{"thing_id": 7}]


async def test_approved_tool_executes_exactly_once():
    """批准后续跑：工具执行**一次**。

    重复执行是最难发现的一类 bug——删除执行两次在单条记录上看起来「成功了」，
    在批量操作上会翻倍。
    """
    agent = _build_agent()
    first = await agent.run("删掉 7")
    assert EXECUTED == []

    history = first.all_messages()
    pending = all_deferred_ids(history)
    assert pending == {"call-fixed"}

    second = await agent.run(
        message_history=history,
        deferred_tool_results=build_resume_results_for({"call-fixed": True}, history),
    )

    assert isinstance(second.output, str)
    assert EXECUTED == [{"thing_id": 7}]
    assert "deleted 7" in second.output


async def test_denied_tool_never_executes():
    """拒绝：工具一次都不跑，模型收到「被拒绝」而不是「工具坏了」。"""
    agent = _build_agent()
    first = await agent.run("删掉 7")

    history = first.all_messages()
    second = await agent.run(
        message_history=history,
        deferred_tool_results=build_resume_results_for({"call-fixed": False}, history),
    )

    assert EXECUTED == []
    assert isinstance(second.output, str)


async def test_missing_decision_counts_as_denial():
    """没有回应 = 拒绝。

    反过来的话，用户关掉页面就等于批准了一次删除。这是这一层最重要的默认值。
    """
    agent = _build_agent()
    first = await agent.run("删掉 7")
    history = first.all_messages()

    results = build_resume_results_for({}, history)

    assert results is not None
    assert EXECUTED == []
    second = await agent.run(message_history=history, deferred_tool_results=results)
    assert EXECUTED == [], "空决定必须等价于拒绝"
    assert isinstance(second.output, str)


async def test_denied_result_text_tells_the_model_not_to_retry():
    """拒绝文案必须说清「是用户拒绝，不是工具坏了」。

    说成故障的话，模型会重试同一个调用（再弹一次审批框），或者编一个「已完成」的回答。
    """
    assert "用户拒绝" in DENIED_RESULT
    assert "不要重试" in DENIED_RESULT


# ---------------------------------------------------------------------
# 续跑辅助
# ---------------------------------------------------------------------
def test_all_deferred_ids_ignores_completed_calls():
    """已有结果的调用不再是待审批：它们不该出现在续跑决定里。"""
    history = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(
            parts=[
                ToolCallPart("a", {}, tool_call_id="done-1"),
                ToolCallPart("b", {}, tool_call_id="pending-1"),
            ]
        ),
        ModelRequest(parts=[]),
    ]
    # 手工补一条「a 已有结果」的响应
    from pydantic_ai.messages import ToolReturnPart

    history[2].parts.append(ToolReturnPart(tool_name="a", content="ok", tool_call_id="done-1"))

    assert all_deferred_ids(history) == {"pending-1"}


def test_all_deferred_ids_is_empty_for_resolved_history():
    assert all_deferred_ids([ModelRequest(parts=[UserPromptPart(content="hi")])]) == set()


def test_resume_results_is_none_for_empty_approvals():
    """不是续跑时返回 None：不能让 pydantic-ai 以为这是续跑。"""
    history = [ModelRequest(parts=[UserPromptPart(content="hi")])]

    assert build_resume_results_for({}, history) is None


def test_resume_results_is_none_when_nothing_pending():
    """历史里没有悬空调用：按普通请求处理，不要因为多出无主决定而报错。"""
    history = [ModelRequest(parts=[UserPromptPart(content="hi")])]

    assert build_resume_results_for({"call-1": True}, history) is None


# ---------------------------------------------------------------------
# 历史重建（客户端回放 → pydantic-ai 消息）
# ---------------------------------------------------------------------
def test_tool_calls_are_restored_into_one_response():
    """工具调用与正文必须合成**同一个** `ModelResponse`。

    模型当初就是把它们放在同一轮里输出的；拆成两条会让历史形状与真实轨迹不符，
    pydantic-ai 会报序列非法，或者模型看到两个不同的「上一轮」。
    """
    messages = [
        ChatMessage(role="user", content="删掉退款政策"),
        ChatMessage(
            role="assistant",
            content="好的，我来删。",
            tool_calls=[{"tool_call_id": "c1", "tool_name": "delete_doc", "args": '{"id": 42}'}],
        ),
    ]

    prompt, history = convert_chat_history(messages, resuming=True)

    assert prompt == ""
    assert len(history) == 2
    response = history[1]
    kinds = [part.part_kind for part in response.parts]
    assert kinds == ["text", "tool-call"]
    assert response.parts[1].args == {"id": 42}


def test_resuming_requires_a_final_tool_call():
    """续跑时末条必须是带工具调用的 assistant：否则无从知道要续跑什么。"""
    from app.services.chat_service import InvalidChatHistory

    messages = [ChatMessage(role="user", content="你好")]

    with pytest.raises(InvalidChatHistory):
        convert_chat_history(messages, resuming=True)


def test_broken_tool_args_are_skipped_not_fatal():
    """客户端回放了一条坏 JSON：跳过那次调用，不要让整轮 500。

    这条历史可能是几天前存的，格式在当时合法；一条坏记录不该让整个会话不可用。
    """
    messages = [
        ChatMessage(role="user", content="删掉退款政策"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"tool_call_id": "c1", "tool_name": "delete_doc", "args": "{not json"}],
        ),
    ]

    _, history = convert_chat_history(messages, resuming=True)

    # 只剩 user 那条（assistant 的 parts 全空 → 不产出 ModelResponse）
    assert len(history) == 1


def test_empty_tool_args_become_empty_dict():
    """无参工具：空串是合法入参，不是「解析失败」。"""
    messages = [
        ChatMessage(role="user", content="执行"),
        ChatMessage(
            role="assistant", content="", tool_calls=[{"tool_call_id": "c1", "tool_name": "no_args", "args": ""}]
        ),
    ]

    _, history = convert_chat_history(messages, resuming=True)

    assert history[1].parts[0].args == {}


# ---------------------------------------------------------------------
# 事件契约
# ---------------------------------------------------------------------
def test_approval_event_serializes_with_descriptions():
    """事件必须带工具说明与风险等级。

    用户只能对着参数做判断，而参数常常看不出后果（`{"id": 42}` 删的是什么？）。
    只给工具名会让审批框变成一次盲点确认。
    """
    from app.schemas.events import ApprovalRequiredEvent, ApprovalToolCall

    event = ApprovalRequiredEvent(
        tool_calls=[
            ApprovalToolCall(
                tool_call_id="c1",
                tool_name="delete_doc",
                args='{"id": 42}',
                description="删除一篇知识库文档（不可撤销）",
                risk_level="dangerous",
            )
        ]
    )

    payload = event.model_dump(mode="json")

    assert payload["type"] == "approval_required"
    assert payload["tool_calls"][0]["description"].startswith("删除")
    assert payload["tool_calls"][0]["risk_level"] == "dangerous"


def test_approval_event_is_a_chat_event():
    """必须在 `ChatEvent` 联合里，否则 SSE 序列化会把它当成未知类型。"""
    # 用 TypeAdapter 反序列化验证判别键已注册
    from pydantic import TypeAdapter

    from app.schemas.events import ApprovalRequiredEvent, ChatEvent

    parsed = TypeAdapter(ChatEvent).validate_python({"type": "approval_required", "tool_calls": []})
    assert isinstance(parsed, ApprovalRequiredEvent)


# ---------------------------------------------------------------------
# 请求契约
# ---------------------------------------------------------------------
def test_request_rejects_approvals_for_unknown_calls():
    """审批 id 必须在历史里真的存在。

    否则 pydantic-ai 拿不到对应的调用，会抛一个用户完全看不懂的内部错误。
    """
    from app.schemas.chat import ChatRequest

    with pytest.raises(ValueError, match="不在本次对话历史中"):
        ChatRequest(
            messages=[{"role": "user", "content": "hi"}],
            approvals={"ghost": True},
        )


def test_request_allows_assistant_final_message_when_resuming():
    """续跑时末条是带工具调用的 assistant：那一条没有产生新的用户提问。"""
    from app.schemas.chat import ChatRequest

    request = ChatRequest(
        messages=[
            {"role": "user", "content": "删掉退款政策"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"tool_call_id": "c1", "tool_name": "delete_doc", "args": "{}"}],
            },
        ],
        approvals={"c1": True},
    )

    assert request.is_resuming_approval is True
    assert request.pending_approval_ids == {"c1"}


def test_request_still_requires_user_final_message_otherwise():
    """普通请求仍必须以 user 结尾：末条不是 user 就不是一次新的提问。"""
    from app.schemas.chat import ChatRequest

    with pytest.raises(ValueError, match="最后一条消息必须是 user 消息"):
        ChatRequest(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
        )


def test_tool_calls_only_allowed_on_assistant_messages():
    """user 消息带工具调用等于伪造模型行为：模型会以为自己上一轮说过那些话。"""
    from app.schemas.chat import ChatMessage

    with pytest.raises(ValueError, match="只有 assistant 消息可以携带 tool_calls"):
        ChatMessage(
            role="user",
            content="hi",
            tool_calls=[{"tool_call_id": "c1", "tool_name": "x", "args": "{}"}],
        )


def test_replayed_tool_calls_are_capped():
    """上限防的是篡改客户端塞几千条来放大请求体。"""
    from app.schemas.chat import MAX_REPLAYED_TOOL_CALLS, ChatMessage

    with pytest.raises(ValueError, match="不能超过"):
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {"tool_call_id": f"c{i}", "tool_name": "x", "args": "{}"} for i in range(MAX_REPLAYED_TOOL_CALLS + 1)
            ],
        )

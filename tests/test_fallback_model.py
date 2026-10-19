"""备用模型（`FallbackModel`）的切换行为。

装配层（`agent_factory`）负责把主/备模型包成 `FallbackModel`，切换判定来自
`_build_fallback_handler`——这里验证它接上之后真的「瞬时失败切、客户端错误不切」，
包括流式路径。
"""

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from app.services.agent_factory import _build_fallback_handler


def _failing_model(status):
    """打开请求/流时立刻抛出指定状态码的模型。"""

    def fail(messages, info):
        raise ModelHTTPError(status_code=status, model_name="primary")

    return FunctionModel(fail, stream_function=fail, model_name="primary")


def _agent(status):
    model = FallbackModel(
        _failing_model(status),
        TestModel(custom_output_text="from-fallback", model_name="fallback"),
        fallback_on=_build_fallback_handler("primary", "fallback"),
    )
    return Agent(model)


@pytest.mark.parametrize("status", [500, 503])
def test_transient_failures_switch_to_the_fallback_model(status):
    assert _agent(status).run_sync("你好").output == "from-fallback"


def test_rate_limit_switches_to_the_fallback_model():
    assert _agent(429).run_sync("你好").output == "from-fallback"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_do_not_switch_models(status):
    """换模型救不回请求本身的问题，直接按分类文案失败。"""
    with pytest.raises(ModelHTTPError) as info:
        _agent(status).run_sync("你好")

    assert info.value.status_code == status


async def test_streaming_failure_before_the_first_event_switches():
    """生产走 `run_stream_events`：流还没吐任何内容时切换，不会重复输出。"""
    agent = _agent(503)

    async with agent.run_stream("你好") as result:
        assert await result.get_output() == "from-fallback"

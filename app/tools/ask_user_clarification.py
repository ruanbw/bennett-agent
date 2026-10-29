"""用户意图澄清/提问工具（pydantic-ai 工具包装）。

校验规则的唯一实现在 `app/runtime/clarification.py`：工具与 `event_translation._clarification_from`
必须共用它，否则工具报错但弹窗已经弹了，模型带新 `tool_call_id` 重试会绕过去重，用户看到两个弹窗。
"""

from pydantic_ai import RunContext

from app.runtime.clarification import (
    MAX_OPTION_CHARS,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    MIN_OPTIONS,
    normalize_clarification,
)


async def ask_user_clarification(
    ctx: RunContext[None],
    question: str,
    options: list[str],
    allow_custom_input: bool = True,
) -> str:
    """当用户意图模糊、缺少必要信息、存在多种理解路径或分支选择时，向用户发起澄清提问。

    前端以弹窗/选项卡片展示，用户可点选或手动输入。

    Args:
        question: 针对不明确之处的具体问题，最多 500 字符（例如：“您是指生成 React 组件还是 Vue 组件？”）
        options: 候选项，2~10 个、每个最多 100 字符（例如：["React 组件", "Vue 组件"]）
        allow_custom_input: 是否允许用户输入自定义文字补充说明（默认为 True）
    """
    normalized = normalize_clarification(question, options)
    if isinstance(normalized, str):
        return normalized

    opts_text = ", ".join(f'"{option}"' for option in normalized.options)
    # 文案必须如实反映 allow_custom_input：写「可以选择或输入」而实际只能选，模型会把问题写
    # 得更开放（反正用户可以补充），用户却发现自己只能点选项。
    how = "供用户点击选择或自行输入" if allow_custom_input else "供用户点击选择（不允许自定义输入）"
    return (
        f"已向用户发起意图确认提问：{normalized.question}。候选选项包括：[{opts_text}]。"
        f"前端已唤起弹窗{how}，等待用户在下一轮对话中确认。"
    )


__all__ = [
    "MAX_OPTION_CHARS",
    "MAX_OPTIONS",
    "MAX_QUESTION_CHARS",
    "MIN_OPTIONS",
    "ask_user_clarification",
    "normalize_clarification",
]

"""澄清提问的入参校验：工具与事件翻译层共用这一份规则。

规则若分两处实现，会出现「工具报错但弹窗已经弹了」：模型带新 `tool_call_id` 重试会绕过去重，
用户看到两个弹窗。所以 `normalize_clarification` 是唯一实现，pydantic-ai 的工具包装在
`app/tools/ask_user_clarification.py`。
"""

from dataclasses import dataclass

#: 输入上限：问题与选项会被原样渲染进前端弹窗，这些数字也是写给模型的自我约束。超限返回
#: 错误文案而不是抛异常、也不静默截断——截断会把问题截成半句话，让用户对着残缺的问题做选择。
MAX_QUESTION_CHARS = 500
MAX_OPTIONS = 10
MAX_OPTION_CHARS = 100

#: 最少候选项。1 个选项的弹窗等于强迫用户确认，2 个才是「选择」。
MIN_OPTIONS = 2


@dataclass(frozen=True, slots=True)
class ClarificationArgs:
    """清洗后的澄清参数；只有 `normalize_clarification` 能构造出它。

    拿到实例就意味着已经通过全部校验，调用方不再需要重复判断边界。
    """

    question: str
    options: list[str]


def normalize_clarification(question: object, options: object) -> ClarificationArgs | str:
    """清洗并校验澄清参数：通过返回 `ClarificationArgs`，不通过返回给人看的错误文案。

    返回文案而非抛异常——这条工具失败不该中止整轮，模型看到原因可以自己改参数。顺序有意义：
    去空白 → 非空与长度 → 选项类型 → 去空去重保序（`dict.fromkeys`）→ 数量 → 单项长度。
    """
    if not isinstance(question, str) or not question.strip():
        return "错误: 提问内容 (question) 不能为空。"

    cleaned_question = question.strip()
    if len(cleaned_question) > MAX_QUESTION_CHARS:
        return f"错误: 提问内容 (question) 不能超过 {MAX_QUESTION_CHARS} 字符。"

    if not isinstance(options, (list, tuple)):
        return "错误: 候选项列表 (options) 必须为列表或元组。"

    unique_options = list(
        dict.fromkeys(option.strip() for option in options if isinstance(option, str) and option.strip())
    )
    if len(unique_options) < MIN_OPTIONS:
        return f"错误: 候选项列表 (options) 必须至少包含 {MIN_OPTIONS} 个不同的非空选项。"
    if len(unique_options) > MAX_OPTIONS:
        return f"错误: 候选项数量 (options) 不能超过 {MAX_OPTIONS} 个。"
    if any(len(option) > MAX_OPTION_CHARS for option in unique_options):
        return f"错误: 单个候选项 (option) 不能超过 {MAX_OPTION_CHARS} 字符。"

    return ClarificationArgs(question=cleaned_question, options=unique_options)


__all__ = [
    "MAX_OPTION_CHARS",
    "MAX_OPTIONS",
    "MAX_QUESTION_CHARS",
    "MIN_OPTIONS",
    "ClarificationArgs",
    "normalize_clarification",
]

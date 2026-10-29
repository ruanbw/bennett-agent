"""用户意图澄清/提问工具。

`normalize_clarification` 是校验规则的唯一实现，工具与 `event_translation._clarification_from` 都必须
调它：规则不一致时工具报错但弹窗已经弹了，模型带新 `tool_call_id` 重试会绕过去重，用户看到两个弹窗。
"""

from dataclasses import dataclass

from pydantic_ai import RunContext

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

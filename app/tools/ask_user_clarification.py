"""用户意图澄清/提问工具"""

from pydantic_ai import RunContext

#: 输入上限：问题与选项会被原样渲染进前端弹窗，这些数字也是写给模型的自我约束。
#: 超限返回错误文案而不是抛异常、也不静默截断——截断会把问题截成半句话，让用户
#: 对着一个残缺的问题做选择。
MAX_QUESTION_CHARS = 500
MAX_OPTIONS = 10
MAX_OPTION_CHARS = 100


async def ask_user_clarification(
    ctx: RunContext[None],
    question: str,
    options: list[str],
    allow_custom_input: bool = True,
) -> str:
    """当用户意图模糊、缺少必要关键信息、存在多种理解路径或分支选择时，向用户发起澄清提问。

    前端将以交互弹窗/选项卡片的形式展示给用户，供用户快速点击选择答案或手动输入自定义内容。

    Args:
        question: 针对用户意图不明确之处提出的具体问题，最多 500 字符（例如：“您是指生成 React 组件还是 Vue 组件？”）
        options: 供用户快速点选的候选项列表，2~10 个、每个最多 100 字符（例如：["React 组件", "Vue 组件", "原生 JS"]）
        allow_custom_input: 是否允许用户输入自定义文字补充说明（默认为 True）
    """
    if not isinstance(question, str) or not question.strip():
        return "错误: 提问内容 (question) 不能为空。"

    if len(question.strip()) > MAX_QUESTION_CHARS:
        return f"错误: 提问内容 (question) 不能超过 {MAX_QUESTION_CHARS} 字符。"

    if not isinstance(options, (list, tuple)):
        return "错误: 候选项列表 (options) 必须为列表或元组。"

    cleaned_options = [opt.strip() for opt in options if isinstance(opt, str) and opt.strip()]
    unique_options = list(dict.fromkeys(cleaned_options))
    if len(unique_options) < 2:
        return "错误: 候选项列表 (options) 必须至少包含 2 个不同的非空选项。"
    if len(unique_options) > MAX_OPTIONS:
        return f"错误: 候选项数量 (options) 不能超过 {MAX_OPTIONS} 个。"
    if any(len(opt) > MAX_OPTION_CHARS for opt in unique_options):
        return f"错误: 单个候选项 (option) 不能超过 {MAX_OPTION_CHARS} 字符。"

    opts_text = ", ".join(f'"{opt}"' for opt in unique_options)
    return (
        f"已向用户发起意图确认提问：{question.strip()}。候选选项包括：[{opts_text}]。"
        "前端已唤起弹窗供用户选择或输入，等待用户在下一轮对话中确认。"
    )

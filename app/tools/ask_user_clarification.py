"""用户意图澄清/提问工具。

## 为什么校验逻辑抽成一个纯函数

服务端有**两个**地方需要判断「这次澄清请求是否有效」，它们必须给出同样的结论：

1. 工具本身（本文件）——决定回灌给模型什么文案；
2. `app/services/chat_service.py::_clarification_from`——决定是否把 `tool_call` 的入参
   翻译成发给前端的 `clarification` 事件。

两边规则不一致时的表现很难看：模型传了 600 字问题，工具返回「不能超过 500 字符」的
错误，**但前端已经收到了那个弹窗**（翻译层只校验了「非空」与「至少 2 个选项」）。
模型收到错误后会重试，带上新的 `tool_call_id`，去重机制拦不住，用户看到**第二个弹窗**。

因此 `normalize_clarification` 是这套规则的唯一实现，两边都调用它。
"""

from dataclasses import dataclass

from pydantic_ai import RunContext

#: 输入上限：问题与选项会被原样渲染进前端弹窗，这些数字也是写给模型的自我约束。
#: 超限返回错误文案而不是抛异常、也不静默截断——截断会把问题截成半句话，让用户
#: 对着一个残缺的问题做选择。
MAX_QUESTION_CHARS = 500
MAX_OPTIONS = 10
MAX_OPTION_CHARS = 100

#: 最少候选项。1 个选项的弹窗等于强迫用户确认，2 个才是「选择」。
MIN_OPTIONS = 2


@dataclass(frozen=True, slots=True)
class ClarificationArgs:
    """清洗后的澄清参数。

    只有 `normalize_clarification` 能构造出它，因此拿到实例就意味着**已经通过全部
    校验**——调用方不再需要重复判断边界。
    """

    question: str
    options: list[str]


def normalize_clarification(question: object, options: object) -> ClarificationArgs | str:
    """清洗并校验澄清参数。

    返回 `ClarificationArgs` 表示通过；返回 `str` 是**给人（模型）看的错误文案**，
    而不是抛异常——这条工具失败不该中止整轮，模型看到原因后可以自己改正参数。

    清洗规则（顺序有意义）：
    1. 问题去掉首尾空白后不能为空、不能超过 `MAX_QUESTION_CHARS`；
    2. 选项必须是 list/tuple，非字符串项直接丢弃（模型偶尔会传数字）；
    3. 每项 strip 后丢弃空串；
    4. 去重且**保持原顺序**（`dict.fromkeys`）；
    5. 去重后数量必须在 `MIN_OPTIONS..MAX_OPTIONS`；
    6. 单项长度不超过 `MAX_OPTION_CHARS`。

    注意「先丢弃空串再去重」与「先去重再丢弃空串」等价，但顺序写死在一处，
    避免两个调用点各自实现出细微差异。
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
    """当用户意图模糊、缺少必要关键信息、存在多种理解路径或分支选择时，向用户发起澄清提问。

    前端将以交互弹窗/选项卡片的形式展示给用户，供用户快速点击选择答案或手动输入自定义内容。

    Args:
        question: 针对用户意图不明确之处提出的具体问题，最多 500 字符（例如：“您是指生成 React 组件还是 Vue 组件？”）
        options: 供用户快速点选的候选项列表，2~10 个、每个最多 100 字符（例如：["React 组件", "Vue 组件", "原生 JS"]）
        allow_custom_input: 是否允许用户输入自定义文字补充说明（默认为 True）
    """
    normalized = normalize_clarification(question, options)
    if isinstance(normalized, str):
        return normalized

    opts_text = ", ".join(f'"{option}"' for option in normalized.options)
    # 文案必须**如实反映** allow_custom_input：写「可以选择或输入」而实际只允许选择，
    # 模型会把问题写得更开放（反正用户可以自己补充），用户却发现自己只能点选项。
    how = "供用户点击选择或自行输入" if allow_custom_input else "供用户点击选择（不允许自定义输入）"
    return (
        f"已向用户发起意图确认提问：{normalized.question}。候选选项包括：[{opts_text}]。"
        f"前端已唤起弹窗{how}，等待用户在下一轮对话中确认。"
    )

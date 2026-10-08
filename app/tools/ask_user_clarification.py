"""用户意图澄清/提问工具"""
from pydantic_ai import RunContext


async def ask_user_clarification(
    ctx: RunContext[None],
    question: str,
    options: list[str],
    allow_custom_input: bool = True,
) -> str:
    """当用户意图模糊、缺少必要关键信息、存在多种理解路径或分支选择时，向用户发起澄清提问。
    
    前端将以交互弹窗/选项卡片的形式展示给用户，供用户快速点击选择答案或手动输入自定义内容。
    
    Args:
        question: 针对用户意图不明确之处提出的具体问题（例如：“您是指生成 React 组件还是 Vue 组件？”）
        options: 供用户快速点选的候选项列表（例如：["React 组件", "Vue 组件", "原生 JS"]）
        allow_custom_input: 是否允许用户输入自定义文字补充说明（默认为 True）
    """
    if not question or not question.strip():
        return "错误: 提问内容 (question) 不能为空。"

    cleaned_options = [opt.strip() for opt in options if opt and opt.strip()]
    unique_options = list(dict.fromkeys(cleaned_options))
    if len(unique_options) < 2:
        return "错误: 候选项列表 (options) 必须至少包含 2 个不同的非空选项。"

    opts_text = ", ".join(f'"{opt}"' for opt in unique_options)
    return (
        f"已向用户发起意图确认提问：{question.strip()}。候选选项包括：[{opts_text}]。"
        "前端已唤起弹窗供用户选择或输入，等待用户在下一轮对话中确认。"
    )

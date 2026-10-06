"""数学计算相关工具"""
from pydantic_ai import RunContext


def calculate_expression(ctx: RunContext[None], expression: str) -> str:
    """安全计算基础数学表达式。

    Args:
        expression: 待计算的数学表达式，如 '125 * 8' 或 '(30 + 15) / 5'
    """
    try:
        allowed = set("0123456789+-*/(). ")
        if not all(c in allowed for c in expression):
            return "错误: 表达式包含不支持的字符"
        result = eval(expression, {"__builtins__": {}}, {})
        return f"计算结果: {result}"
    except Exception as e:
        return f"计算失败: {e}"

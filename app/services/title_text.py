"""会话标题的后处理：清洗模型输出与截断兜底。

为什么单独成模块：标题是模型输出，天然带噪音——引号、「标题：」前缀、换行、尾随句号。
此前 `generate_title_from_prompt` 只 `strip(_TITLE_QUOTE_CHARS)` 就返回，于是「标题：向量
数据库选型」这样的结果会被原样存进侧边栏；截断用的是 Python 切片（UTF-16 之外按码点，
但会把 emoji 后面的组合字符切断），而前端 `clampTitle` 是按码点算的，两端口径不一致。

这里是纯函数，不碰网络与数据库，可以直接单测。
"""

#: 模型常见的包裹符号：成对出现时整对剥掉，避免「"标题"」这种残留。
_WRAPPING_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("(", ")"),
    ("（", "）"),
    ("[", "]"),
    ("【", "】"),
)

#: 模型爱加的前缀。全部按「去掉前缀 + 空白」处理，大小写不敏感。
_PREFIXES: tuple[str, ...] = (
    "会话标题：",
    "会话标题:",
    "标题：",
    "标题:",
    "title:",
    "title：",
    "topic:",
    "主题：",
    "主题:",
)

#: 标题结尾常被补上的标点。只剥这些，不动问号/感叹号（它们可能是标题语义的一部分）。
_TRAILING_MARKS = "。：:，,、；;"


def _strip_wrapping(text: str) -> str:
    """反复剥掉成对包裹符号：模型偶尔会输出 `「"标题"」`。"""
    result = text
    changed = True
    while changed and len(result) >= 2:
        changed = False
        for left, right in _WRAPPING_PAIRS:
            if result.startswith(left) and result.endswith(right):
                result = result[len(left) : len(result) - len(right)].strip()
                changed = True
                break
    return result


def _strip_prefix(text: str) -> str:
    lowered = text.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix.lower()):
            return text[len(prefix) :].strip()
    return text


def clamp_title(raw: str, max_chars: int) -> str:
    """按 Unicode 码点截断，不劈开 emoji / 组合字符。

    与前端 `lib/session-list.ts:clampTitle` 同一口径：两端各自截断时，标题长度不能
    因为实现语言不同而不同。
    """
    chars = list(raw)
    if len(chars) <= max_chars:
        return raw
    return "".join(chars[:max_chars]).strip()


def normalize_title(raw: str | None, max_chars: int) -> str:
    """把模型输出清洗成可直接落库的标题。清洗后为空返回空串，由调用方兜底。"""
    if not raw:
        return ""

    # 折叠换行与连续空白：标题是单行文本，多行会被侧边栏截成半截
    text = " ".join(raw.split())
    text = _strip_wrapping(text)
    text = _strip_prefix(text)
    text = _strip_wrapping(text)
    text = text.strip().rstrip(_TRAILING_MARKS).strip()
    return clamp_title(text, max_chars)


def fallback_title(prompt: str, max_chars: int) -> str:
    """生成失败时用首条消息兜底。

    取首行、折叠空白、去掉 Markdown 标题记号（`# 帮我写个函数` → `帮我写个函数`）：
    用户的第一条消息常常直接是 Markdown，占位标题不该带着 `#` 出现在侧边栏。
    """
    first_line = (prompt or "").strip().split("\n", 1)[0]
    cleaned = " ".join(first_line.split()).lstrip("#").strip()
    return clamp_title(cleaned, max_chars)

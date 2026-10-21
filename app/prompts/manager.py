from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import jinja2

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

#: 只把这些后缀当模板。「提示词目录里放一篇 README」不应该被当成模板去编译。
TEMPLATE_SUFFIXES = (".jinja", ".jinja2", ".j2", ".txt")


class PromptTemplateError(ValueError):
    """模板本身有问题（语法错误、找不到、渲染成空）。

    单独一个类型而不是裸 `ValueError`：启动自检要能把这个错误与「数据库连不上」
    之类的启动故障区分开，日志里也直接点明是提示词的问题。
    """


class PromptManager:
    """提示词管理器：基于 Jinja2 加载、缓存与渲染本地模版。"""

    def __init__(self, template_dir: Path | str = PROMPTS_DIR):
        self.template_dir = Path(template_dir)
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.template_dir)),
            #: 提示词是纯文本，不是 HTML；开启自动转义会把 `{{ }}` 之外的内容
            #: （包括正常的引号、尖括号）改写成 HTML 实体，污染提示词。
            #: 若将来用同一实例渲染 HTML，必须换成 autoescape=True 的新实例。
            autoescape=False,
            #: 漏传变量时**报错**，而不是渲染成空串。静默空串会让系统提示词悄悄
            #: 少掉一整段（例如「当前环境: 」），模型行为随之变化却没有任何痕迹；
            #: 显式写了 `| default(...)` 的变量不受影响——那是刻意的兜底。
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, **kwargs: Any) -> str:
        """渲染指定的提示词模板文件，例如 'system_prompt.jinja'。

        渲染结果为空（或只有空白）时抛 `PromptTemplateError`：模型收到一个空系统提示词
        不会报错，只会「行为莫名其妙地变了」，而这类故障在日志里没有任何痕迹——把
        「模板把内容全删了 / 全写进 Jinja 注释了」在启动或首次渲染时就挡住。
        """
        template = self.env.get_template(template_name)
        rendered = template.render(**kwargs).strip()
        if not rendered:
            raise PromptTemplateError(
                f"提示词模板 {template_name} 渲染结果为空：检查是否把内容写进了 Jinja 注释，"
                "或所有内容都被条件分支挡掉了。"
            )
        return rendered

    def template_names(self) -> list[str]:
        """目录下的模板名（相对路径、按字典序）。"""
        names = [name for name in self.env.list_templates() if Path(name).suffix.lower() in TEMPLATE_SUFFIXES]
        return sorted(names)

    def validate_templates(self) -> list[str]:
        """编译全部模板，返回模板名列表；有语法错误时抛 `PromptTemplateError`。

        `get_template` 会真正 parse 一遍，所以语法错误（少一个 `endif`、过滤器名拼错）
        在这里就会暴露——而不是等到某位用户发出第一条消息时，在预算预检里炸成一句
        「请求处理失败」。**启动自检调用它**（见 `app/server.py`）。
        """
        names = self.template_names()
        errors: list[str] = []
        for name in names:
            try:
                self.env.get_template(name)
            except jinja2.TemplateError as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise PromptTemplateError("提示词模板编译失败：\n  " + "\n  ".join(errors))
        return names

    def digest(self, template_name: str) -> str:
        """模板内容的短指纹（12 位十六进制）。

        排查「同样的提示词为什么行为变了」时，先说清楚**当时用的是哪一版**：日志里带一个
        指纹，比事后翻 git 历史对时间可靠。取源码而不是渲染结果——渲染结果里有环境名、
        工具清单这些会随配置变化的变量，指纹会失真。
        """
        source = (self.template_dir / template_name).read_text(encoding="utf-8")
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


# 单例全局管理器
prompt_manager = PromptManager()

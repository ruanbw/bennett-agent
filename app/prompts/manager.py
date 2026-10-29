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

    单独一个类型：启动自检要能把它与「数据库连不上」之类的启动故障区分开。
    """


class PromptManager:
    """提示词管理器：基于 Jinja2 加载、缓存与渲染本地模版。"""

    def __init__(self, template_dir: Path | str = PROMPTS_DIR):
        self.template_dir = Path(template_dir)
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.template_dir)),
            # 提示词是纯文本：开启自动转义会把引号、尖括号改写成 HTML 实体，污染提示词。
            autoescape=False,
            #: 漏传变量时报错而不是渲染成空串：静默空串会让系统提示词悄悄少掉一整段；
            #: 显式写了 `| default(...)` 的变量不受影响。
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, **kwargs: Any) -> str:
        """渲染指定模板，例如 'system_prompt.jinja'；结果为空时抛 `PromptTemplateError`。

        空系统提示词不会报错，只会让模型行为莫名其妙地变，而日志里没有痕迹。
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
        """编译全部模板并返回模板名，语法错误则抛 `PromptTemplateError`（启动自检调用它，见 `app/server.py`）。"""
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
        """模板内容的短指纹（12 位十六进制），排查「同样的提示词行为变了」时先说清用的是哪一版。

        取源码而不是渲染结果：渲染结果里有环境名、工具清单，指纹会随配置失真。
        """
        source = (self.template_dir / template_name).read_text(encoding="utf-8")
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


# 单例全局管理器
prompt_manager = PromptManager()

from pathlib import Path
from typing import Any

import jinja2

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


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
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, **kwargs: Any) -> str:
        """渲染指定的提示词模板文件，例如 'system_prompt.jinja'。"""
        template = self.env.get_template(template_name)
        return template.render(**kwargs).strip()


# 单例全局管理器
prompt_manager = PromptManager()

"""应用版本号的单一来源。

为什么不再写死：`FastAPI(version="1.0.0")` 与 `pyproject.toml` 的 `0.1.0` 早就对不上了，
`/openapi.json` 里的 `info.version` 因此与设置面板、打包产物的版本各说各话。版本是诊断
信息（用户报障第一句通常是「我这是哪个版本」），只能有一个来源。

来源优先级：

1. 已安装包的元数据（`importlib.metadata`）——`uv sync` / `pip install .` 后的正常路径；
2. 元数据不可用（源码树里直接 `python main.py`：uv 的运行环境并未把本项目装成包）时，
   读仓库根目录 `pyproject.toml` 的 `[project].version`；
3. 两者都拿不到才退回 `0.0.0+unknown`——**不猜**，让「这个进程不是从安装包起来的」
   一眼可见。

不引入 `tomllib`：项目最低支持 Python 3.10，而 `tomllib` 是 3.11 才进标准库；为了读一行
版本号引入第三方 TOML 解析器不划算，用一条锚定 `[project]` 段的窄正则即可（读不到就
走兜底，不会因为文件里别处写了别的 version 而读错）。
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: 分发包名，与 pyproject.toml 的 `[project].name` 一致。
PACKAGE_NAME = "bennett-agent"
#: 拿不到任何来源时的占位值（既没安装、也没有 pyproject）。
UNKNOWN_VERSION = "0.0.0+unknown"

#: 仓库根目录的 pyproject.toml（`app/version.py` 的上两级）。
PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"

_PROJECT_SECTION = re.compile(r"^\[project\]\s*$", re.MULTILINE)
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def _version_from_pyproject(path: Path = PYPROJECT_PATH) -> str | None:
    """从 pyproject 的 `[project]` 段读版本号；文件缺失或读不到时返回 None。

    只看 `[project]` 段内部的 `version = "..."`：`[tool.*]` 里也可能出现同名字段，
    全文搜会把它们误当成应用版本。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    section = _PROJECT_SECTION.search(text)
    if section is None:
        return None
    rest = text[section.end() :]
    # 截到下一个段头，避免读到别的段里的 version
    next_section = re.search(r"^\[", rest, re.MULTILINE)
    if next_section is not None:
        rest = rest[: next_section.start()]
    matched = _VERSION_LINE.search(rest)
    return matched.group(1) if matched else None


def get_app_version() -> str:
    """返回应用版本号；所有来源都不可用时返回 `UNKNOWN_VERSION`。"""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - 元数据损坏也只影响展示，不该让服务起不来
        pass
    return _version_from_pyproject() or UNKNOWN_VERSION


APP_VERSION = get_app_version()

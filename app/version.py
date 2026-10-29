"""应用版本号的单一来源。

版本是诊断信息，只能有一个来源：优先已安装包的元数据，源码树里直接跑时读 pyproject 的 `[project].version`，
都拿不到才退回 `0.0.0+unknown`（不猜）；不用 `tomllib`（3.11 才有），改用锚定 `[project]` 段的窄正则。
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: 分发包名，与 pyproject.toml 的 `[project].name` 一致。
PACKAGE_NAME = "bennett-agent"
#: 两个来源都拿不到时的占位值（不猜）。
UNKNOWN_VERSION = "0.0.0+unknown"

#: 仓库根目录的 pyproject.toml（`app/version.py` 的上两级）。
PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"

_PROJECT_SECTION = re.compile(r"^\[project\]\s*$", re.MULTILINE)
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def _version_from_pyproject(path: Path = PYPROJECT_PATH) -> str | None:
    """从 pyproject 的 `[project]` 段读版本号；文件缺失或读不到时返回 `None`。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    section = _PROJECT_SECTION.search(text)
    if section is None:
        return None
    rest = text[section.end() :]
    # 截到下一个段头，别读进别的段
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

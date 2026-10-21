"""备用模型配置口径（`app/services/fallback_config.py`）。

三件事必须锁住：空值/同名视为不启用、找不到时**告警**（否则运维以为自己有兜底）、
告警同一组合只记一次（不能每个请求刷一行）。
"""

import logging

import pytest

from app.config import settings
from app.services import fallback_config
from app.services.fallback_config import (
    configured_fallback_name,
    find_fallback_model_id,
    reset_fallback_warnings,
    warn_fallback_unavailable,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_fallback_warnings()
    monkeypatch.setattr(settings, "fallback_model_name", None)
    yield
    reset_fallback_warnings()


def test_blank_or_missing_fallback_means_disabled(monkeypatch):
    assert configured_fallback_name("openai/gpt-4o") is None

    monkeypatch.setattr(settings, "fallback_model_name", "   ")
    assert configured_fallback_name("openai/gpt-4o") is None


def test_same_name_as_primary_means_disabled(monkeypatch):
    """同名兜底只会对同一个模型再打一次，还让错误变成异常组。"""
    monkeypatch.setattr(settings, "fallback_model_name", "openai/gpt-4o")

    assert configured_fallback_name("openai/gpt-4o") is None


def test_case_is_not_normalised(monkeypatch):
    """模型 id 由网关定义，`gpt-4o` 与 `GPT-4O` 是两个模型，后端不做猜测。"""
    monkeypatch.setattr(settings, "fallback_model_name", "openai/GPT-4O")

    assert configured_fallback_name("openai/gpt-4o") == "openai/GPT-4O"


def test_configured_name_is_returned_stripped(monkeypatch):
    monkeypatch.setattr(settings, "fallback_model_name", "  openai/backup  ")

    assert configured_fallback_name("openai/gpt-4o") == "openai/backup"


def test_unavailable_fallback_is_warned_once(caplog, monkeypatch):
    """配了但用不上必须告警；否则「主模型 503 直接失败」在现场看不出是配置问题。"""
    monkeypatch.setattr(settings, "fallback_model_name", "openai/backup")

    with caplog.at_level(logging.WARNING, logger="app.services.fallback_config"):
        warn_fallback_unavailable("openai/gpt-4o", "openai/backup", "同一提供商内没有这个已启用的模型")
        warn_fallback_unavailable("openai/gpt-4o", "openai/backup", "同一提供商内没有这个已启用的模型")

    messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert len(messages) == 1
    assert "openai/backup" in messages[0]
    assert "不会切换" in messages[0]


def test_warning_is_repeated_for_a_different_pair(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.fallback_config"):
        warn_fallback_unavailable("openai/gpt-4o", "openai/backup", "原因")
        warn_fallback_unavailable("openai/gpt-4o-mini", "openai/backup", "原因")

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_find_fallback_model_id_matches_exactly():
    ids = ["openai/gpt-4o", "openai/backup", "openai/GPT-4O"]

    assert find_fallback_model_id(ids, "openai/backup") == "openai/backup"
    assert find_fallback_model_id(ids, "openai/gpt-4o") == "openai/gpt-4o"
    assert find_fallback_model_id(ids, "openai/missing") is None
    assert find_fallback_model_id([], "openai/backup") is None


def test_module_exposes_reset_for_tests():
    assert callable(fallback_config.reset_fallback_warnings)

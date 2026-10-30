"""产物导出：对象键必须带上 `S3_PREFIX` 并过校验，下载名必须是最终文件名。

两处都是「看着已经实现、实际是空的」保护：`S3_PREFIX` 改了没反应、`_validate_key` 没人调、
下载下来是一串 `sbx-artifact-XXXX`。它们不需要真实对象存储，所以用替身直接锁。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.services.sandbox import artifacts
from app.services.sandbox.lease import LeaseView

LEASE = LeaseView(
    id="sbx_test_lease",
    user_id=uuid.UUID(int=1),
    session_id="sess_test",
    provider="fake",
    provider_ref="fake-1",
    state="ready",
    artifact_uri=None,
)


class _RecordingStore:
    """只记录上传入参；`put_file` 的契约就是「本地文件 → 对象键」。"""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str]] = []

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        # 临时文件在 `_upload` 返回前就会被删，所以这里必须当场读出来。
        assert Path(path).exists(), "上传时文件必须还在"
        stored = Path(path).read_bytes()
        self.uploads.append((key, path, content_type))
        assert stored


def test_artifact_key_starts_with_the_configured_prefix():
    """`S3_PREFIX` 必须真的进键：按前缀授权的最小权限桶策略依赖它。"""
    key = artifacts._artifact_key(LEASE, "workspace.tar.gz")  # noqa: SLF001

    assert key == f"{settings.s3_prefix}/{LEASE.user_id}/{LEASE.id}/workspace.tar.gz"


def test_artifact_key_handles_an_empty_prefix(monkeypatch):
    """前缀置空时不留前导斜杠：`/user/...` 是绝对路径风格的键，在自建网关上会写到桶外。"""
    monkeypatch.setattr(settings, "s3_prefix", "")

    key = artifacts._artifact_key(LEASE, "manifest.txt")  # noqa: SLF001

    assert key == f"{LEASE.user_id}/{LEASE.id}/manifest.txt"


def test_artifact_key_is_validated(monkeypatch):
    """前缀被配成 `../etc` 时必须当场失败，而不是写出一条能逃出桶目录的键。"""
    monkeypatch.setattr(settings, "s3_prefix", "../etc")

    with pytest.raises(ValueError):
        artifacts._artifact_key(LEASE, "manifest.txt")  # noqa: SLF001


async def test_upload_uses_the_final_filename_as_the_download_name(monkeypatch):
    """上传接口用 `Path(path).name` 拼 `Content-Disposition`。

    临时名会让用户把 `workspace.tar.gz` 下载成 `sbx-artifact-XXXX`（重名时还会被浏览器加 `(1)`）。
    """
    store = _RecordingStore()
    monkeypatch.setattr(artifacts, "get_object_store", lambda: store)
    key = artifacts._artifact_key(LEASE, "workspace.tar.gz")  # noqa: SLF001

    assert await artifacts._upload(key, b"payload", "application/gzip") == key  # noqa: SLF001

    assert len(store.uploads) == 1
    uploaded_key, path, content_type = store.uploads[0]
    assert uploaded_key == key
    assert content_type == "application/gzip"
    assert Path(path).name == "workspace.tar.gz"
    assert not Path(path).exists(), "临时文件用后即删"

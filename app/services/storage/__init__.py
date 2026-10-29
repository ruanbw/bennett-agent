"""对象存储抽象：沙箱产物只经由这一层进出（唯一用法见 `app/services/sandbox/artifacts.py`）。

不写本地盘：多实例各写各的盘，回收循环在哪个实例跑产物就落在哪，用户换实例就 404；容器重建即丢。
抽象只覆盖真实用到的 put/get/delete/exists/presign，不做通用文件系统——每多一个动作就多一个语义面。
"""

from __future__ import annotations

from app.services.storage.base import (
    ObjectStore,
    ObjectStoreError,
    StorageNotConfigured,
    sanitize_display_name,
)
from app.services.storage.s3 import S3ObjectStore, get_object_store, reset_object_store

__all__ = [
    "ObjectStore",
    "ObjectStoreError",
    "S3ObjectStore",
    "StorageNotConfigured",
    "get_object_store",
    "reset_object_store",
    "sanitize_display_name",
]

"""对象存储抽象：上传文件的原件**只**经由这一层进出。

## 为什么要有这层薄抽象

宿主机上直接写 `uploads/` 目录在开发时最省事，但它把三个问题拖到生产才暴露：

1. 多实例时各写各的本地盘，用户上传后重定向到另一个实例就 404；
2. 备份、容量、生命周期管理都得自己实现（而对象存储这些都是现成的）；
3. 容器重建即丢数据（除非挂卷，而挂卷又回到第 1 条）。

S3 协议把这些统一掉了：本地用 RustFS（或 MinIO），云上用 S3 / OSS / COS 的 S3
兼容端点，代码只认 `endpoint_url + bucket`。

抽象只覆盖本项目真正用到的五个动作（put / get / delete / exists / presign），
不做「通用文件系统」。每多一个动作就多一个必须自己保证语义一致的行为面。
"""

from __future__ import annotations

from app.services.storage.base import (
    ObjectStore,
    ObjectStoreError,
    StorageNotConfigured,
    build_storage_key,
    sanitize_display_name,
)
from app.services.storage.s3 import S3ObjectStore, get_object_store, reset_object_store

__all__ = [
    "ObjectStore",
    "ObjectStoreError",
    "S3ObjectStore",
    "StorageNotConfigured",
    "build_storage_key",
    "get_object_store",
    "reset_object_store",
    "sanitize_display_name",
]

"""对象存储的接口与纯函数工具（不含任何 S3 具体实现）。
键构造与文件名清洗放这里是为了能被单测直接覆盖：它们出错都是安全类问题，不需要真实存储。
"""

from __future__ import annotations

import re
from typing import Protocol

#: 对象键允许的字符集。用户文件名永远不进键，这里只校验服务端生成的结果。
_SAFE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")

#: 控制字符（含换行、制表、DEL）与路径分隔符：日志注入与路径穿越的常见载体。
_CONTROL_OR_SEPARATOR = re.compile(r"[\x00-\x1f\x7f\\/]+")

#: 展示用文件名的最大长度。256 是多数文件系统的上限，留出后缀后取 200。
MAX_DISPLAY_NAME_LENGTH = 200


class ObjectStoreError(RuntimeError):
    """对象存储操作失败（网络、权限、桶不存在）。"""


class StorageNotConfigured(ObjectStoreError):
    """对象存储凭据缺失：这是一个部署配置问题，不是用户错误。"""


class ObjectStore(Protocol):
    """本项目用到的最小对象存储接口；全部 async，同步的 boto3 由实现用线程池卸载。
    接口层不把「同步还是异步」泄露给调用方。"""

    async def ensure_bucket(self) -> None:
        """确保桶存在（幂等）。未配置 `S3_CREATE_BUCKET` 时实现可以为空。"""
        ...

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        """把本地文件（而不是内存里的 bytes）上传上去。
        入参是路径：上传接口已把请求体流式落到临时文件，这样内存占用与文件大小无关。
        """
        ...

    async def get_bytes(self, key: str) -> bytes:
        """读取整个对象。仅用于小对象（当前实现里没有调用方；产物走预签名直链）。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象。对象不存在时**不报错**（删除是幂等操作）。"""
        ...

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        ...

    async def stat_size(self, key: str) -> int | None:
        """对象大小（字节）；对象不存在时返回 `None` 而不是抛异常。
        生命周期规则常比数据库行先清掉对象，租约表里留着一个已消失的键是正常情况。
        """
        ...

    async def presigned_get_url(self, key: str, expires_in: int) -> str:
        """生成有时效的下载直链（预签名，不占应用带宽）。
        代价是链接在有效期内可被转发，所以有效期默认只有 5 分钟（`S3_PRESIGN_EXPIRY_SECONDS`）。
        """
        ...

    async def health(self) -> tuple[bool, str]:
        """就绪探针用：返回 `(是否可用, 失败原因)`。不抛异常。"""
        ...


def sanitize_display_name(raw: str) -> str:
    """把用户提供的文件名清洗成可安全展示/写日志的形式。
    只保证展示安全，绝不拿来拼路径（键由 `artifacts.py:_artifact_key` 生成）：清洗漏一项就是写任意键。
    """
    name = (raw or "").strip()
    # 只取最后一段：反斜杠也要处理。
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_OR_SEPARATOR.sub("_", name)
    name = name.strip(" .")
    if not name:
        return "unnamed"

    if len(name) <= MAX_DISPLAY_NAME_LENGTH:
        return name

    # 保留后缀按「后缀长度」判，不按点的位置：限制点的位置会让长文件名恰恰丢掉后缀。
    dot = name.rfind(".")
    suffix = name[dot:] if dot > 0 and len(name) - dot <= 16 else ""
    return name[: MAX_DISPLAY_NAME_LENGTH - len(suffix)] + suffix


def _validate_key(key: str) -> None:
    """校验生成的对象键：白名单不够，`../etc/<uuid>` 同样符合字符集，所以要逐段检查。
    自建网关、反代与把桶挂成本地目录的工具会把它当路径，后果是写到别处。
    """
    segments = key.split("/")
    if (
        not _SAFE_KEY_PATTERN.match(key)
        or key.startswith("/")
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError(f"生成的对象键不合法：{key!r}")

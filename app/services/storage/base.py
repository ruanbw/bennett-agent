"""对象存储的接口与纯函数工具（不含任何 S3 具体实现）。

把「键怎么构造」「文件名怎么清洗」放在这里，是为了让它们可以被单测直接覆盖：
这两件事出错的方式都是安全类问题（对象键注入、路径穿越），而它们不需要一个真实的
对象存储才能验证。
"""

from __future__ import annotations

import re
import uuid
from typing import Protocol

#: 对象键允许的字符集。**用户文件名永远不进键**，这里只用于校验生成结果。
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
    """本项目用到的最小对象存储接口。

    所有方法都是 async 的。S3 客户端是同步的（boto3），实现里用线程池卸载——
    接口层不把「同步还是异步」这个实现细节泄露给调用方。
    """

    async def ensure_bucket(self) -> None:
        """确保桶存在（幂等）。未配置 `S3_CREATE_BUCKET` 时实现可以为空。"""
        ...

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        """把本地文件（而不是内存里的 bytes）上传上去。

        用文件而不是 bytes 作为入参：上传接口把请求体**流式落到临时文件**（边写边算
        sha256、边判断大小上限），所以拿到的是路径。这样内存占用与文件大小无关，
        25MB 的上限才不会变成「同时来 20 个人就 OOM」。
        """
        ...

    async def get_bytes(self, key: str) -> bytes:
        """读取整个对象。仅用于解析——调用方保证对象大小受 `UPLOAD_MAX_BYTES` 约束。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象。对象不存在时**不报错**（删除是幂等操作）。"""
        ...

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        ...

    async def presigned_get_url(self, key: str, expires_in: int) -> str:
        """生成有时效的下载直链。

        用预签名而不是「后端代理下载」：后者要为一个纯 IO 动作占用应用进程与带宽，
        而对象存储本来就是为这个场景设计的。代价是链接在有效期内可被转发——
        所以默认只有 5 分钟（`S3_PRESIGN_EXPIRY_SECONDS`）。
        """
        ...

    async def health(self) -> tuple[bool, str]:
        """就绪探针用：返回 `(是否可用, 失败原因)`。不抛异常。"""
        ...


def sanitize_display_name(raw: str) -> str:
    """把用户提供的文件名清洗成可安全展示/写日志的形式。

    只做「展示安全」，**不做**「拿来拼路径」——对象键由 `build_storage_key` 生成。
    这两件事混在一起是路径穿越的经典来源：一旦有人用清洗后的名字拼键，任何清洗漏项
    都直接变成写任意键。

    处理的四类东西：
    - 路径部分（`../../etc/passwd` → `passwd`）：浏览器偶尔会带完整路径；
    - 控制字符与路径分隔符：写日志时会伪造行、在部分后端会变成子目录；
    - 尾部的点与空格：Windows 上会被静默去掉，导致「文件名看起来不一样」；
    - 超长：主体截断（保留后缀，否则用户看不出这是什么文件）。
    """
    name = (raw or "").strip()
    # 只取最后一段：反斜杠也要处理（Windows 客户端常见）
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_OR_SEPARATOR.sub("_", name)
    name = name.strip(" .")
    if not name:
        return "unnamed"

    if len(name) <= MAX_DISPLAY_NAME_LENGTH:
        return name

    # 保留后缀：截断后仍有 `.pdf` 才能让用户与前端识别类型。
    # 判定的是**后缀长度**（`.pdf` 是 4 个字符），不是点的位置——把点限制在开头
    # 16 个字符内会让 `xxxx….pdf` 这种长文件名丢掉后缀，而它们恰恰最需要截断。
    dot = name.rfind(".")
    suffix = name[dot:] if dot > 0 and len(name) - dot <= 16 else ""
    return name[: MAX_DISPLAY_NAME_LENGTH - len(suffix)] + suffix


def build_storage_key(prefix: str, user_id: uuid.UUID, *, extension: str = "") -> str:
    """生成对象键：`{prefix}/{user_id}/{uuid4}{.ext}`。

    **只有服务端生成的部分参与键**：用户 id（隔离维度）+ 随机 uuid（不可猜）。
    用户文件名一个字符都不进键，因此 `../`、控制字符、超长中文都影响不到对象路径。

    保留扩展名（而不是完全省略）是为了运维直接看桶时能分辨文件类型；它来自
    **白名单校验过**的扩展名，不是用户原始文件名。
    """
    suffix = f".{extension.lstrip('.').lower()}" if extension else ""
    key = f"{prefix.strip('/')}/{user_id}/{uuid.uuid4().hex}{suffix}"
    _validate_key(key)
    return key


def _validate_key(key: str) -> None:
    """校验生成的对象键。

    字符白名单本身不够：`../etc/<uuid>` 完全符合 `[A-Za-z0-9._/-]`，但它是一次
    路径穿越。所以还要逐段检查——没有空段、没有 `.`/`..`、不以斜杠开头。
    这类键通常不会被 S3 解释成路径（对象存储的键是扁平字符串），但自建网关、
    反向代理、以及把桶挂成本地目录的工具会，而后果是写到意料之外的位置。
    """
    segments = key.split("/")
    if (
        not _SAFE_KEY_PATTERN.match(key)
        or key.startswith("/")
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError(f"生成的对象键不合法：{key!r}")

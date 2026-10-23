"""S3 兼容对象存储实现（本地 RustFS / 云上 S3、OSS、COS 的 S3 端点）。

## 为什么用 boto3，而不是 aioboto3 / minio SDK

- **boto3**：AWS 官方 SDK，S3 协议的参考实现。它的同步调用用
  `asyncio.to_thread` 卸载到线程池即可，这与仓库里其他同步阻塞调用（限流、Argon2
  校验）用的是同一种模式，没有引入第二套并发模型。而且 botocore 已经作为 litellm
  的传递依赖存在于锁文件里——新增的依赖面接近于零。
- **aioboto3 / aiobotocore**：真正的异步，但它把 botocore 钉在特定版本区间上，
  与 litellm 这类会升级 botocore 的库共存时经常解不出依赖。为了少几次线程切换引入了
  一个长期的版本冲突源，不划算。
- **minio SDK**：对 MinIO/RustFS 友好，但它不是标准 S3 客户端，换到 AWS 或 OSS 时
  又要改代码。我们需要的恰恰是「同一套代码换个端点就能跑」。

## 为什么是 path-style

RustFS / MinIO / 多数自建网关只支持 `endpoint/bucket/key`。virtual-hosted style
（`bucket.endpoint/key`）需要为每个桶配 DNS 与 TLS 通配证书——那是云上 S3 的形态。
默认 `S3_USE_PATH_STYLE=true`，用 AWS 官方 S3 时设为 false。

## 客户端是长生命周期的

boto3 的 client **是线程安全的**（resource 不是），可以全局复用：每次调用都新建
client 会重复加载服务模型（几十毫秒）并重建连接池。这里用一个惰性单例。

## 超时与重试

`connect_timeout` / `read_timeout` 从配置读，重试用 botocore 的 standard 模式
（3 次，带抖动）。对象存储的失败大部分是瞬时网络问题，而上传 25MB 的 read_timeout
必须比普通 API 宽松——但在请求路径上仍然不能无限等。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.storage.base import (
    ObjectStoreError,
    StorageNotConfigured,
    sanitize_display_name,
)

logger = logging.getLogger(__name__)

#: 进程内单例（惰性创建，见模块 docstring）。
_client: Any = None

#: 建桶时用到的区域约束：`us-east-1` 不能传 `CreateBucketConfiguration`，
#: 传了会被 S3 拒绝为 `InvalidLocationConstraint`。
_DEFAULT_REGION = "us-east-1"


def _require_credentials() -> None:
    """凭据缺失时尽早失败，并说清缺什么。

    不在这里给默认值：RustFS 的默认凭据（rustfsadmin/rustfsadmin）是公开的，
    静默兜底会让一个「忘了配 .env」的部署看起来能跑，实际任何人都能读写它的桶。
    """
    missing = [
        name
        for name, value in (
            ("S3_ACCESS_KEY", settings.s3_access_key),
            ("S3_SECRET_KEY", settings.s3_secret_key),
            ("S3_BUCKET", settings.s3_bucket),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise StorageNotConfigured(
            f"对象存储未配置：缺少 {', '.join(missing)}。"
            "本地开发请确认 .env 里的 S3_* 与 docker compose 的 rustfs 服务一致。"
        )


def _build_client() -> Any:  # noqa: ANN401 - boto3 client 没有可用的类型桩
    _require_credentials()

    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=settings.s3_connect_timeout,
            read_timeout=settings.s3_read_timeout,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={
                "addressing_style": "path" if settings.s3_use_path_style else "auto",
                # 新版 SDK 默认会给上传加 CRC 校验头；部分自建网关（含一些版本的
                # MinIO/RustFS）对 `x-amz-checksum-*` 支持不完整，会返回
                # `XAmzContentSHA256Mismatch` 或直接 400。我们的完整性校验靠
                # sha256（入库时算过），因此这里关掉 SDK 的自动校验。
                "payload_signing_enabled": False,
            },
        ),
    )


def get_client() -> Any:  # noqa: ANN401
    """取（或创建）共享 boto3 client。"""
    global _client
    if _client is None:
        _client = _build_client()
    return _client


def reset_object_store() -> None:
    """丢弃缓存的 client（测试改配置后调用）。"""
    global _client
    _client = None


class S3ObjectStore:
    """`ObjectStore` 的 S3 实现。所有 boto3 调用都在线程池里执行。"""

    def __init__(self, bucket: str | None = None, prefix: str | None = None) -> None:
        self.bucket = bucket or settings.s3_bucket
        self.prefix = prefix or settings.s3_prefix

    # -- 内部：把同步调用卸载到线程池 ---------------------------------
    async def _call(self, operation: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """执行一次 boto3 调用，把 botocore 异常翻译成本项目自己的异常。

        为什么要翻译：`ClientError` 的报文是 JSON 字符串，直接冒到 HTTP 层会变成
        500 加一坨用户看不懂的东西。这里保留原始信息在日志里，对外只给一句能行动的
        描述（桶不存在 / 权限不足 / 网关不可达）。
        """
        client = get_client()

        def run() -> Any:  # noqa: ANN401
            return getattr(client, operation)(**kwargs)

        try:
            return await asyncio.to_thread(run)
        except StorageNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一归类后再抛出
            code = _error_code(exc)
            logger.warning("对象存储操作失败: %s (code=%s)", operation, code, exc_info=True)
            if code in {"NoSuchBucket", "404"}:
                raise ObjectStoreError(
                    f"桶 {self.bucket!r} 不存在：请先创建（或设 S3_CREATE_BUCKET=true 让服务自建）。"
                ) from exc
            if code in {"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise ObjectStoreError("对象存储拒绝访问：请检查 S3_ACCESS_KEY / S3_SECRET_KEY 与桶策略。") from exc
            raise ObjectStoreError(f"对象存储不可用（{operation} 失败，code={code}）。") from exc

    async def ensure_bucket(self) -> None:
        if not settings.s3_create_bucket:
            return

        client = get_client()

        def run() -> None:
            try:
                client.head_bucket(Bucket=self.bucket)
                return
            except Exception as exc:  # noqa: BLE001 - 只在「不存在」时继续建桶
                if _error_code(exc) not in {"404", "NoSuchBucket"}:
                    raise
            kwargs: dict[str, Any] = {"Bucket": self.bucket}
            # us-east-1 是默认 region，不能带 LocationConstraint
            if settings.s3_region and settings.s3_region != _DEFAULT_REGION:
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.s3_region}
            client.create_bucket(**kwargs)
            logger.info("已创建对象存储桶: %s", self.bucket)

        try:
            await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                return
            logger.warning("确保桶存在失败: bucket=%s code=%s", self.bucket, code, exc_info=True)
            raise ObjectStoreError(f"确保桶 {self.bucket!r} 存在失败（code={code}）。") from exc

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        # `upload_file` 会自动选择单次 PUT 或分片上传（>8MB 时），并把大文件切成
        # 多线程分片——比手写 put_object 更稳，也避免单请求超时。
        # 下载时用附件名（而不是键里的 uuid），否则用户看到的是一串十六进制。
        disposition_name = _quote_filename(sanitize_display_name(Path(path).name))
        await self._call(
            "upload_file",
            Filename=str(Path(path)),
            Bucket=self.bucket,
            Key=key,
            ExtraArgs={
                "ContentType": content_type,
                "ContentDisposition": f"attachment; filename*=UTF-8''{disposition_name}",
            },
        )

    async def get_bytes(self, key: str) -> bytes:  # noqa: ANN401
        response = await self._call("get_object", Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            return await asyncio.to_thread(body.read)
        finally:
            await asyncio.to_thread(body.close)

    async def delete(self, key: str) -> None:
        # S3 的 DeleteObject 对不存在的键返回 204，天然幂等
        await self._call("delete_object", Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        client = get_client()

        def run() -> bool:
            try:
                client.head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                    return False
                raise

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError("检查对象是否存在失败。") from exc

    async def presigned_get_url(self, key: str, expires_in: int) -> str:
        client = get_client()
        try:
            return await asyncio.to_thread(
                client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError("生成下载链接失败。") from exc

    async def health(self) -> tuple[bool, str]:
        """就绪探针：只 head 一次桶。

        不 list 对象：桶里可能有几十万个键，list 会在权限或性能上都变成负担，
        而探针要回答的只是「凭据有效且桶可达」。
        """
        try:
            client = get_client()
        except StorageNotConfigured:
            return False, "not_configured"
        try:
            await asyncio.to_thread(client.head_bucket, Bucket=self.bucket)
        except Exception as exc:  # noqa: BLE001
            return False, _error_code(exc) or type(exc).__name__
        return True, ""


def _error_code(exc: BaseException) -> str:
    """从 botocore 的 ClientError 里取错误码；其他异常退回异常类名。

    botocore 只有在装了它时才存在，因此延迟导入：这个模块在纯单测里也会被导入
    （例如测试文件名的清洗），不该因为一条诊断路径而硬依赖可选的错误类型。
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code") or "")
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, dict) and metadata.get("HTTPStatusCode"):
            return str(metadata["HTTPStatusCode"])
    return type(exc).__name__


def _quote_filename(name: str) -> str:
    """按 RFC 5987 编码 Content-Disposition 里的文件名。"""
    from urllib.parse import quote

    return quote(name, safe="")


def get_object_store() -> S3ObjectStore:
    """取共享的对象存储实例（无状态，可安全复用）。"""
    return S3ObjectStore()

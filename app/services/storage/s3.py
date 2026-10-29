"""S3 兼容对象存储实现（本地 RustFS / 云上 S3、OSS、COS 的 S3 端点）。

用 boto3：同步调用丢 to_thread，与仓库其他阻塞调用同一套并发模型；aioboto3 会把 botocore 钉版本、与 litellm 冲突。
默认 path-style（自建网关只认 `endpoint/bucket/key`）；client 线程安全，全局复用一个惰性单例。
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

#: 进程内惰性单例：每次新建 client 都会重新加载服务模型并重建连接池。
_client: Any = None

#: `us-east-1` 不能传 `CreateBucketConfiguration`，传了会被拒为 `InvalidLocationConstraint`。
_DEFAULT_REGION = "us-east-1"


def _require_credentials() -> None:
    """凭据缺失时尽早失败，并说清缺什么。

    不给默认值：RustFS 的默认凭据是公开的，静默兜底会让忘了配 .env 的部署变成谁都能读写。
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
                # 新版 SDK 默认加 CRC 校验头，部分自建网关（MinIO/RustFS）对 `x-amz-checksum-*`
                # 支持不完整会 400；完整性已由入库时算过的 sha256 保证。
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

        `ClientError` 的报文是 JSON 字符串，冒到 HTTP 层就是看不懂的 500；原文进日志，对外只给能行动的描述。
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
        # `upload_file` 会自动选单次 PUT 或分片上传（>8MB），比手写 put_object 稳，也不会单请求超时。
        # 下载时给附件名（而不是键里的 uuid），否则用户看到的是一串十六进制。
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

    async def stat_size(self, key: str) -> int | None:
        client = get_client()

        def run() -> int | None:
            try:
                response = client.head_object(Bucket=self.bucket, Key=key)
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                    return None
                raise
            size = response.get("ContentLength")
            return int(size) if size is not None else None

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError("读取对象大小失败。") from exc

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

        不 list 对象：几十万个键在权限与性能上都是负担，而探针只回答「凭据有效且桶可达」。
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
    """从 botocore 的 `ClientError` 里取错误码；其他异常退回异常类名。"""
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

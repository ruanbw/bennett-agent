"""上传文件的校验、解析、编排与 worker。

分成四个模块，是为了让每层都能被单独测试，也避免「一个模块同时做 HTTP、存储、解析」：

    validators  类型/大小/内容判定（纯函数，无 IO）
    parsers     字节 → 文本（CPU，放进线程池执行）
    service     编排：落临时文件 → 对象存储 → 建 pending 记录
    worker      Postgres 队列消费：领取 → 解析 → 入库 → 更新状态
"""

from app.services.uploads.service import (
    UploadOutcome,
    UploadQuotaExceeded,
    UploadTooLarge,
    create_upload,
    delete_upload,
    get_upload,
    list_uploads,
)
from app.services.uploads.validators import UploadValidationError, detect_kind

__all__ = [
    "UploadOutcome",
    "UploadQuotaExceeded",
    "UploadTooLarge",
    "UploadValidationError",
    "create_upload",
    "delete_upload",
    "detect_kind",
    "get_upload",
    "list_uploads",
]

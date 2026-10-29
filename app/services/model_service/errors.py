"""模型服务对外的错误类型。
单列成模块是为了让 `registry` 与 `outbound_guard` 都能引用而不形成循环导入；与 HTTP 的映射
（400/404/409/502）在 `app/api/admin.py`，这里只管「服务层能给出哪种确定结论」。
"""


class ProviderNotFoundError(LookupError):
    """提供商不存在（管理接口据此返回 404）。"""


class DuplicateModelError(ValueError):
    """同一提供商下已经存在同名模型。"""


class InvalidModelStateError(ValueError):
    """模型状态组合不合法，例如把已停用的模型设为默认。"""


class ProviderFetchError(RuntimeError):
    """无法从提供商读取模型列表。消息是给管理员看的，不包含上游响应原文。"""


class ApiKeyRequiredError(ValueError):
    """更换 Base URL 时必须重新填写 API Key。消息直接回给管理员。"""

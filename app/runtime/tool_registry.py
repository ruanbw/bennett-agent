"""基于 PydanticAI 原生 Tool 的统一工具注册中心与元数据治理模块。"""

from collections.abc import Callable
from enum import Enum
from typing import Any

from pydantic_ai.tools import (
    DocstringFormat,
    GenerateJsonSchema,
    GenerateToolJsonSchema,
    Tool,
    ToolAgentDepsT,
    ToolFuncEither,
    ToolParams,
    ToolPrepareFunc,
)


class RiskLevel(str, Enum):
    """工具风险等级，分级在 `app/runtime/approval.py` 真正生效。
    READ_ONLY / INTERACTIVE 直接执行；SAFE_WRITE / DANGEROUS 执行前必须拿到用户批准（HITL），未批准则工具
    一次都不执行；分界是「能不能撤销」——本项目删了没有回收站，所以低风险写入也要先问。
    """

    READ_ONLY = "read_only"
    """只读操作，无外部副作用（如联网检索、数据查询）。"""

    INTERACTIVE = "interactive"
    """交互确认，向用户发起澄清提问并等待下一轮响应。"""

    SAFE_WRITE = "safe_write"
    """幂等或低风险的写入/更新操作。"""

    DANGEROUS = "dangerous"
    """破坏性或高风险操作（如删库、高额计费、敏感变更），建议触发 HITL 审核。"""


class ToolCategory(str, Enum):
    """工具分类，只是元数据：没有代码按它拦截，唯一执行闸门是 `RiskLevel`。"""

    SEARCH = "search"
    """外部检索与信息抓取。"""

    INTERACTION = "interaction"
    """与终端用户的意图确认、交互和提示。"""

    DATA = "data"
    """内部数据库与仓储操作。"""

    SYSTEM = "system"
    """系统管理、配置查询与基础设施。"""


class ManagedTool(Tool[ToolAgentDepsT]):
    """PydanticAI 原生 `Tool` 的受控扩展：保留原生签名推导 / JSON Schema / 超时重试，
    另加治理元数据（分类、风险等级、标签、启用检查器）与向后兼容属性。
    """

    def __init__(
        self,
        function: ToolFuncEither[ToolAgentDepsT, ToolParams],
        *,
        name: str | None = None,
        description: str | None = None,
        category: ToolCategory = ToolCategory.SYSTEM,
        risk_level: RiskLevel = RiskLevel.READ_ONLY,
        tags: list[str] | None = None,
        enabled_checker: Callable[[], bool] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        takes_ctx: bool | None = None,
        prepare: ToolPrepareFunc[ToolAgentDepsT] | None = None,
        docstring_format: DocstringFormat = "auto",
        require_parameter_descriptions: bool = False,
        schema_generator: type[GenerateJsonSchema] = GenerateToolJsonSchema,
        strict: bool | None = None,
        sequential: bool = False,
        requires_approval: bool = False,
        metadata: dict[str, Any] | None = None,
    ):
        meta = dict(metadata or {})
        meta.setdefault("category", category.value if isinstance(category, Enum) else str(category))
        meta.setdefault("risk_level", risk_level.value if isinstance(risk_level, Enum) else str(risk_level))
        meta.setdefault("tags", list(tags or []))

        super().__init__(
            function,
            name=name,
            description=description,
            timeout=timeout,
            max_retries=max_retries,
            takes_ctx=takes_ctx,
            prepare=prepare,
            docstring_format=docstring_format,
            require_parameter_descriptions=require_parameter_descriptions,
            schema_generator=schema_generator,
            strict=strict,
            sequential=sequential,
            requires_approval=requires_approval,
            metadata=meta,
        )
        self.category = category
        self.risk_level = risk_level
        self.tags = list(tags or [])
        self.enabled_checker = enabled_checker

    @property
    def __name__(self) -> str:
        """兼容外部代码/测试对 `tool.__name__` 的反射。"""
        return self.name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """支持直接将工具对象作为普通函数调用，透传给底层实现。"""
        return self.function(*args, **kwargs)

    @property
    def is_enabled(self) -> bool:
        """判断当前环境或配置下此工具是否激活。"""
        if self.enabled_checker is None:
            return True
        try:
            return bool(self.enabled_checker())
        except Exception:
            return False


class ToolRegistry:
    """工具注册中心：编目、启用检查、按分类 / 风险等级 / 标签过滤与装配。"""

    def __init__(self) -> None:
        self._tools: dict[str, ManagedTool[Any]] = {}

    def register(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        category: ToolCategory = ToolCategory.SYSTEM,
        risk_level: RiskLevel = RiskLevel.READ_ONLY,
        tags: list[str] | None = None,
        enabled_checker: Callable[[], bool] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        takes_ctx: bool | None = None,
        prepare: ToolPrepareFunc[Any] | None = None,
        requires_approval: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], ManagedTool[Any]]:
        """装饰器：将函数封装并注册为 ManagedTool。"""

        def decorator(fn: Callable[..., Any]) -> ManagedTool[Any]:
            tool = ManagedTool(
                fn,
                name=name,
                description=description,
                category=category,
                risk_level=risk_level,
                tags=tags,
                enabled_checker=enabled_checker,
                timeout=timeout,
                max_retries=max_retries,
                takes_ctx=takes_ctx,
                prepare=prepare,
                requires_approval=requires_approval,
                metadata=metadata,
            )
            self._tools[tool.name] = tool
            return tool

        return decorator

    def register_tool(self, tool: ManagedTool[Any]) -> ManagedTool[Any]:
        """直接注册一个已构造的 ManagedTool 实例。"""
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> ManagedTool[Any] | None:
        """按工具唯一名称检索工具对象。"""
        return self._tools.get(name)

    def all_tools(self) -> list[ManagedTool[Any]]:
        """返回全量已注册工具列表（包含当前环境未激活的工具）。"""
        return list(self._tools.values())

    def list_tools(
        self,
        *,
        category: ToolCategory | None = None,
        risk_level: RiskLevel | None = None,
        tag: str | None = None,
        only_enabled: bool = False,
    ) -> list[ManagedTool[Any]]:
        """根据分类、风险等级、标签以及启用状态筛选工具。"""
        results: list[ManagedTool[Any]] = []
        for tool in self._tools.values():
            if only_enabled and not tool.is_enabled:
                continue
            if category is not None and tool.category != category:
                continue
            if risk_level is not None and tool.risk_level != risk_level:
                continue
            if tag is not None and tag not in tool.tags:
                continue
            results.append(tool)
        return results

    def build_tools(self) -> list[ManagedTool[Any]]:
        """生产装配：按当前运行时状态动态过滤出可供 Agent 挂载的工具列表。"""
        return [tool for tool in self._tools.values() if tool.is_enabled]

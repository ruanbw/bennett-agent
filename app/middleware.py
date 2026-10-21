"""HTTP 中间件：请求体大小上限。

## 为什么需要它

Starlette / uvicorn **默认不限制请求体大小**。本项目的 JSON body 校验发生在 Pydantic
解析**之后**（例如同步接口的单条 payload 上限是 256 KB），也就是说一个 10MB 的请求
会先被完整读进内存、解析成对象，然后才被告知「这条消息太大了」。注册完全开放，任何
人都能反复发送超大 body，把单进程的内存打满（H-02）。

这里在 ASGI 层做两道检查：

1. `Content-Length` 声明超限 → 不读 body，直接 413；
2. 分块传输（没有 Content-Length）→ 边读边累计，超过上限立即中止并 413。

上限是 `settings.max_request_body_bytes`（默认 16 MB），足够容纳对话历史的 400k
字符与正常的同步批次，同时把「一次请求能占多少内存」变成有界值。反向代理上还应再
配一层更小的值（见 `.env.example`）。
"""

from __future__ import annotations

import json
from typing import Any


#: 请求体超限时抛出的内部异常；只在本模块内用于中断读取。
class _BodyTooLarge(Exception):
    pass


def _declared_content_length(scope: dict[str, Any]) -> int | None:
    """解析 `Content-Length`；缺失或非法返回 None（非法值交给上层框架处理）。"""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def _send_413(send: Any) -> None:
    body = json.dumps({"detail": "请求体过大，已拒绝"}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class MaxBodySizeMiddleware:
    """限制单个 HTTP 请求体大小；超限返回 413。

    纯 ASGI 实现（不用 `BaseHTTPMiddleware`）：后者会额外包一层 task，且对 SSE 这类
    流式响应有已知的缓冲/取消问题，不适合本服务的 `/api/chat`。
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes 必须 >= 1")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _send_413(send)
            return

        received = 0
        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # 抛到本中间件的 try/except；应用（FastAPI/Starlette）不会把它
                    # 当成正常输入继续处理。
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            # 只有在响应尚未开始时才能补 413；已经开始写响应说明 body 是在响应
            # 中途被读的（本服务没有这种端点），此时只能让连接按异常关闭。
            if not response_started:
                await _send_413(send)

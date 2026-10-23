"""前端构建产物的同源托管。

## 为什么需要它

开发期前端跑在 Vite 自己的端口上、通过 `server.proxy` 把 `/api` 转到后端，这是同源的
（见 `frontend/vite.config.js` 的注释）。但**生产不能这么部署**：要么起一个 Nginx 反代，
要么让后端自己托管 `frontend/dist`。

这个模块选后者，理由有三条：

1. **少一个必须配置正确的组件。** 反代方案要让 `/api` 与静态资源都转发到同一域名，
   还要把 cookie 的 Path/Domain/SameSite 对齐——任何一处漏掉的表现都是「登录成功了，
   但下一个请求又是未登录」，是最难查的一类故障。
2. **部署形态已经是单机单进程。** 既然 SQLite 和进程内指标都接受了这个前提，再多一个
   反代容器并不会让它变得更可扩展。
3. **CORS 可以整块关掉。** 同源之后 `CORS_ALLOWED_ORIGINS` 留空即可，攻击面少一块。

## 挂载条件是「产物真的存在」，而不是某个开关

Dockerfile 里前端是**先构建再复制**的，所以镜像里一定有 `frontend/dist`。而开发者在本地
跑 `uv run python main.py --web` 时通常没构建过前端——此时后端必须照旧只提供 `/api`，
否则访问 `/` 会 404，而真正的用法是再开一个 Vite。所以这里按目录存在与否自动判断：
**有产物就托管，没有就只做 API**。要显式关掉（产物在但你要用反代）时设 `SERVE_FRONTEND=false`。

## 四条容易做错的规则

**1. `/api/*` 的 404 必须是 404，不能被 SPA 回退成 `index.html`。**

单页应用的标准做法是「找不到路径就返回 index.html，让前端路由接管」。但如果无差别回退，
`GET /api/typo` 会返回 200 + 一坨 HTML——前端 `fetch` 拿到 200 却 JSON 解析失败，日志里
没有任何 404，一个打错的接口看起来「成功了」。所以回退只对**非 `/api`** 路径生效。

**2. `/assets/` 下找不到的文件必须是 404，不能回退成 `index.html`。**

`/assets/app-<hash>.js` 这类文件名带内容哈希，换版本后旧页面引用的旧文件已经不存在。
如果回退成 HTML，浏览器会把它当 JS 解析并报 `Unexpected token '<'`——这个报错完全指不到
真正原因（「你部署的是新版本」）。给一个干脆的 404，浏览器才有机会触发刷新。

**3. 静态资源长缓存，`index.html` 不缓存。**

`/assets/*` 是内容哈希命名的，内容变了文件名就变，可以放心缓存一年；而 `index.html` 是
**版本切换的唯一入口**，缓存住就等于用户永远用旧版本。用 `no-cache`（可以存，但每次回源
确认）而不是 `no-store`：回源确认能拿到 304，`no-store` 每次都重传整份 HTML。

`app/middleware.py` 的 `SecurityHeadersMiddleware` 只对 `/api` 前缀加 `no-store`，静态文件
不受影响——这一点是刻意的，否则下面这两条缓存策略都会被覆盖掉。

**4. 路径穿越必须挡住。**

`../../etc/passwd` 经 URL 编码后在 Starlette 里会解码成真实路径。`Path.resolve()` 之后
必须确认它仍落在产物目录内，否则这个端点等于一个任意文件读取。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)

#: 前端构建产物里入口文件的固定名字。
INDEX_FILE = "index.html"

#: 带内容哈希的静态资源目录（Vite 默认输出到 `dist/assets/`）。
ASSETS_DIR = "assets"

#: 入口文件不缓存：它是版本切换的唯一入口。
INDEX_CACHE_CONTROL = "no-cache"

#: 内容哈希资源的缓存时长。内容变了文件名就变，所以一年是安全的。
ASSETS_CACHE_CONTROL = "public, max-age=31536000, immutable"


def resolve_frontend_dir(configured: str | None) -> Path | None:
    """定位前端构建产物目录；不存在或没配时返回 None。

    `configured` 为空时用默认位置 `frontend/dist`——相对**项目根**而不是当前工作目录：
    从别的目录启动服务时 `cwd` 会变，按 cwd 找会静默找不到。

    只认「目录里真的有 index.html」：空目录、只有 `.gitkeep` 的目录都不算。否则托管逻辑
    会把所有页面路由到 404，比干脆不托管更难查。
    """
    base = Path(__file__).resolve().parent.parent  # 项目根（app/ 的上一级）
    candidate = Path(configured).expanduser() if configured else base / "frontend" / "dist"
    if not candidate.is_absolute():
        candidate = base / candidate
    if not (candidate / INDEX_FILE).is_file():
        return None
    return candidate


def mount_frontend(app: FastAPI, directory: Path) -> None:
    """把构建产物挂到根路径，并为 SPA 路由加回退。

    **必须在所有 API 路由注册之后调用**：后注册的路由优先匹配，而这个 catch-all 会
    匹配一切。放在前面会把 `/api/*`、`/docs`、`/health` 全部吞掉。
    """
    root = directory.resolve()

    def _cache_for(relative: str) -> str:
        return ASSETS_CACHE_CONTROL if relative.startswith(f"{ASSETS_DIR}/") else INDEX_CACHE_CONTROL

    def _served(relative: str) -> FileResponse:
        """按相对路径返回产物文件，并带上与目录匹配的缓存策略。"""
        return FileResponse(root / relative, headers={"Cache-Control": _cache_for(relative)})

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa_fallback(path: str) -> FileResponse | JSONResponse:
        """未命中的路径：有对应文件就给它，否则回退到 SPA 入口。

        规则见模块 docstring 的四条；这里把两类「必须 404」的情况放在最前面，避免它们
        被后面的回退逻辑覆盖。
        """
        # 规则 1：不存在的 API 端点不能被回退成 HTML
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        # 规则 4：拒绝目录穿越。编码形态（`%2e%2e%2f`）此时已被 Starlette 解码，
        # 所以判断的是解码后的真实路径。
        candidate = (root / path).resolve() if path else root
        try:
            candidate.relative_to(root)
        except ValueError:
            logger.warning("拒绝疑似目录穿越的静态请求: %r", path)
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        if path and candidate.is_file():
            return _served(candidate.relative_to(root).as_posix())

        # 规则 2：带哈希的资源缺失要给 404，不能拿 HTML 冒充 JS
        if path == ASSETS_DIR or path.startswith(f"{ASSETS_DIR}/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        # 规则 3：其余一切交给 SPA 入口（前端路由的合法入口，状态码是 200）
        return _served(INDEX_FILE)

    logger.info("已托管前端构建产物: %s", directory)

# Bennett Agent —— 单镜像部署
#
# ## 为什么是一个镜像而不是两个
#
# 后端可以自己托管前端构建产物（`app/frontend.py`），所以生产形态是**同源单进程**：
# 一个容器同时提供 `/api/*` 与静态页面。这样做的理由不是「省一个容器」，而是：
#
# 1. 会话走 HttpOnly cookie。跨源部署要求 CORS、`allow_credentials`、SameSite 三处
#    同时对齐，任何一处漏掉的表现都是「登录成功了，但下一个请求又是未登录」——
#    最难查的那类故障。同源则三处都不需要配。
# 2. 反代方案还得额外维护一份 Nginx 配置（SSE 要关 buffering、超时要放大），
#    而那份配置的正确性没有任何测试覆盖。放进镜像里反而少一个会坏的组件。
#
# ## 为什么是多阶段构建
#
# 前端需要 Node 才能构建，但运行时完全用不到它。多阶段让最终镜像里只有 Python、
# 依赖与本仓库代码（约 200MB，而不是把 node_modules 一起塞进去的 800MB+）。
#
# ## 构建缓存怎么排
#
# 依赖文件的复制单独成一层，且排在做 `COPY . .` **之前**：改一行 Python 代码不会让
# `uv sync` 那一层失效。反过来（先 copy 全部再装依赖）会让每次改代码都重装一遍依赖，
# 构建时间从 5 秒变成 2 分钟。

# ---------------------------------------------------------------------
# 阶段 1：构建前端
# ---------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build

# 只复制依赖清单：`npm ci` 的缓存层只随 lockfile 变化
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` 而不是 `npm install`：严格按 lockfile 装，且 lockfile 与 package.json
# 不一致时直接失败。用 `install` 会悄悄改写 lockfile，构建结果不再可复现。
RUN npm ci

COPY frontend/ ./
# `npm run build` = `tsc --noEmit && vite build`：类型错误会让构建失败，
# 而不是产出一份「能跑但类型是错的」的产物。
RUN npm run build


# ---------------------------------------------------------------------
# 阶段 2：运行时
# ---------------------------------------------------------------------
# 版本与 `.python-version` / `pyproject.toml` 的 `requires-python` 保持一致（3.10）。
# 镜像里跑 3.12 而本地与 CI 跑 3.10 会制造一类查不清的差异——代码里就有一处明确依赖
# 3.10 行为（没有 `asyncio.timeout`，一律用 `asyncio.wait_for`）。要么统一版本，
# 要么就得在两边分别验证，而那等于两套测试。
FROM python:3.10-slim AS runtime

# - PYTHONUNBUFFERED：日志不缓冲。容器里缓冲住日志意味着崩溃时看不到最后几行，
#   而那几行通常是唯一能说明原因的东西。
# - PYTHONDONTWRITEBYTECODE：运行时不需要 .pyc，写了只是占空间。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# uv 从官方镜像复制：比 `pip install uv` 快，且不占构建缓存的一层。
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# 非 root 运行。容器默认是 root，而本服务会写 SQLite 文件——root 写出来的文件
# 在宿主上属于 root，之后运维想改权限就得 sudo。
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

WORKDIR /app

# 依赖层：只复制清单 + lockfile，改业务代码不会让它失效
COPY pyproject.toml uv.lock ./
# `--frozen`：严格按 uv.lock 装，不重新解析。少了它，构建时可能拉到与本地不同的
# 版本，「本地测试通过、镜像里行为不同」会变成一类查不清的问题。
# `--no-dev`：测试/lint 依赖不进运行镜像。
RUN uv sync --frozen --no-dev

# 业务代码放在依赖之后：这一层最容易变，放最后能让上面的层全部命中缓存
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY evals/ ./evals/
COPY scripts/ ./scripts/
COPY main.py pyproject.toml ./

# 前端构建产物（阶段 1 的输出）。后端按目录存在与否自动决定是否托管
# （见 `app/frontend.py`），所以这里放进默认位置即可。
COPY --from=frontend /build/dist ./frontend/dist

# SQLite 默认落在工作目录（`./bennett.db`）。建一个独立的数据目录并声明为卷，
# 这样数据库与代码分开：镜像可以随时重建，数据不会跟着丢。
RUN mkdir -p /data && chown -R app:app /data /app
ENV DATABASE_URL="sqlite+aiosqlite:////data/bennett.db"

USER app

# 只能监听 0.0.0.0：容器里的 127.0.0.1 只接受本机连接，宿主映射的端口进不来。
# 这是一条经典的「容器起来了但访问不了」的原因。
EXPOSE 8000

# 就绪探针用 `/health/ready`（真的 ping 数据库），不是 `/health`。
# 用 liveness 做 readiness 会让「进程活着但数据库坏了」的实例留在负载均衡里。
# `python -c` 而不是 curl：slim 镜像里没有 curl，为一个探针装它不值得。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"

# 走 `main.py --web`，**不直接调 uvicorn**。
#
# 这不是偷懒：`main.py` 在 uvicorn.run 之前做了三件容器里同样必需的事——
# 1. `configure_logging()`：装上带 `[request_id]` 的格式与 RequestIdFilter。直接起
#    uvicorn 会得到一套完全不同的日志格式，而且**没有请求 ID**，报障时无法回查。
# 2. `sanitize_no_proxy_env()`：清理坏掉的 `no_proxy`。它在解析参数之前跑，因为坏的
#    no_proxy 会让所有 `trust_env` 的 HTTP 客户端在构造阶段就失败。
# 3. `proxy_headers=False`、`timeout_graceful_shutdown=5`、`log_config=None` 已经在
#    uvicorn.run 的参数里定死（理由见 `main.py` 的注释），不用在这里重复。
#
# `--host 0.0.0.0` 必须显式给：默认是 127.0.0.1，容器里的回环只接本机连接，
# 宿主映射的端口进不来——「容器起来了但访问不了」的经典原因。
#
# 用 exec 形式（JSON 数组）让 python 直接成为 PID 1：shell 形式（CMD "..."）会多一层
# /bin/sh，SIGTERM 到不了 python，`docker stop` 只能等到超时后 SIGKILL。
CMD ["python", "main.py", "--web", "--host", "0.0.0.0", "--port", "8000"]

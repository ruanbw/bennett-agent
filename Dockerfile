# Bennett Agent —— 后端镜像
#
# ## 只打包后端
#
# 仓库是**前后端分离**的：本镜像只提供 `/api/*`，不托管任何前端产物。
# 前端构建（`npm run build`）与静态托管是独立的一步——产物交给任意静态服务器或
# CDN，`/api` 由反向代理转给本服务。
#
# 这样切分是有意的：把前端产物塞进后端进程会让「换一次前端」变成「重启一次 API」，
# 也让后端的镜像体积、Node 工具链与前端发版节奏绑在一起。
#
# 跨源部署要点（既然是分离的，就必须面对它）：
# - 会话走 HttpOnly cookie，前端 fetch 要带 `credentials: 'include'`；
# - 后端 CORS 必须同时给出 `allow_credentials=True` 与**明确的** `allow_origins`
#   （浏览器拒绝 `*` + credentials 的组合），对照 `CORS_ALLOWED_ORIGINS`；
# - 前端若与后端不同 site，cookie 的 SameSite 需要按部署形态调整。
#
# 更省事的形态是让 Nginx/网关把 `/api` 与静态页面收在同一个域名下（同源），
# 上面三条就都不需要了——那是部署侧的事，不由这个镜像承担。
#
# ## 为什么是多阶段构建
#
# 依赖与代码分层：改一行 Python 代码不会让依赖层失效。
#
# ## 构建缓存怎么排
#
# 依赖文件的复制单独成一层，且排在做 `COPY . .` **之前**：改一行 Python 代码不会让
# `uv sync` 那一层失效。反过来（先 copy 全部再装依赖）会让每次改代码都重装一遍依赖，
# 构建时间从 5 秒变成 2 分钟。

# ---------------------------------------------------------------------
# 运行时
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

# 非 root 运行。容器默认是 root，而应用会写日志/临时文件——root 写出来的文件
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
# `--chown`：容器以非 root（app）运行，而 COPY 默认把文件留给 root。源文件在宿主上
# 若还是 0600（本仓库里有这类文件），root:0600 的文件对 app 用户就是「不可读」——
# 症状是启动时 `PermissionError: /app/app/config/logging_setup.py`，看着像镜像坏了。
COPY --chown=app:app app/ ./app/
COPY --chown=app:app prompts/ ./prompts/
COPY --chown=app:app evals/ ./evals/
COPY --chown=app:app scripts/ ./scripts/
# 建库脚本必须进镜像：启动时执行 sql/init.sql，缺了它服务起不来。
COPY --chown=app:app sql/ ./sql/
COPY --chown=app:app main.py pyproject.toml ./

USER app

# 只能监听 0.0.0.0：容器里的 127.0.0.1 只接受本机连接，宿主映射的端口进不来。
# 这是一条经典的「容器起来了但访问不了」的原因。
EXPOSE 8000

# 就绪探针用 `/health/ready`（真的 ping 数据库），不是 `/health`。
# 用 liveness 做 readiness 会让「进程活着但数据库坏了」的实例留在负载均衡里。
# `python -c` 而不是 curl：slim 镜像里没有 curl，为一个探针装它不值得。
# 注意探针只检查 database；Redis/对象存储挂了时端点返回 200 + degraded，
# 理由见 settings.readiness_required_dependencies。
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

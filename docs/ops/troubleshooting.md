---
title: 故障排查
description: 启动自检、认证、上游、沙箱、同步、限流的真实报错原文与处理办法。
---

# 故障排查

每条给一个可复制的命令或一个日志关键字。表里「验证」那一列是让你在动手前先确认原因的，不要跳过——这几种症状的表面现象高度相似。

## 启动与配置

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| 启动即退出，日志有 `提示词自检失败，服务不会启动` | `prompts/` 下的 Jinja 模板编译不过，或系统提示词渲染成空串（`PromptTemplateError`） | `grep -n "提示词自检失败" 日志`；`uv run python -c "from app.prompts import prompt_manager; print(prompt_manager.validate_templates())"` | 按错误里点名的模板与行号改 `prompts/system_prompt.jinja`；渲染成空串通常是把内容写进了 Jinja 注释 |
| 启动即退出，日志有 `数据库结构已漂移` | 模型里有这些列、库里没有。`sql/init.sql` 的 `CREATE TABLE IF NOT EXISTS` 对既有表不会加列 | `grep -n "数据库结构已漂移" 日志`，错误里直接给了可复制的 `ALTER TABLE` | 开发库：`docker compose down -v && docker compose up -d postgres`；生产：手工执行错误里列出的 ALTER 语句 |
| 启动即退出，`生产环境必须设置强随机 AUTH_SECRET` | `APP_ENV=production` 且 `AUTH_SECRET` 是占位值、含占位词，或长度 < 32 | `grep -n "AUTH_SECRET" 日志`；`python -c "import secrets; print(secrets.token_urlsafe(48))"` | 换成 ≥32 字符的强随机串。注意：换 `AUTH_SECRET` 会让已加密的提供商 API Key 解不开，需到管理后台重填 |
| 启动即退出，`生产环境必须设置 AUTH_COOKIE_SECURE=true` | 生产环境 cookie 没走 HTTPS | `grep -n "AUTH_COOKIE_SECURE" 日志` | 置 `AUTH_COOKIE_SECURE=true` 并确保前面是 TLS 终止的代理 |
| 启动即退出，`会话 cookie 的 SameSite=None 必须同时设置 AUTH_COOKIE_SECURE=true` | `CORS_ALLOW_CREDENTIALS=true` 时 SameSite 默认解析成 `none`，而 `AUTH_COOKIE_SECURE=false` | `grep -n "SameSite=None" 日志` | 本地 http 调试把 `CORS_ALLOW_CREDENTIALS` 改回 `false`（SameSite 回落到 `lax`），或显式设 `AUTH_COOKIE_SAMESITE=lax`；跨站部署就开 HTTPS |
| 启动即退出，`AUTH_COOKIE_NAME 用了 __Host- 前缀` / `__Secure- 前缀` | cookie 名带前缀但没开 `AUTH_COOKIE_SECURE`，浏览器会直接丢弃这类 cookie | `grep -n "AUTH_COOKIE_NAME" 日志` | 设 `AUTH_COOKIE_SECURE=true`，或改用不带前缀的名字 |
| 启动即退出，`APP_ENV 必须是 … 之一` | 环境名拼错（`Production`、`prod`），拼错会让 `.env.{env}` 静默加载不到 | `grep -n "APP_ENV" 日志` | 只用 `development` / `production` / `staging` / `test`；大小写会被归一化 |
| 启动即退出，`DATABASE_URL 必须是 postgresql+asyncpg://…` | 连接串还写着 sqlite 或同步驱动 | `grep -n "DATABASE_URL" 日志` | 改回 `postgresql+asyncpg://`；本地用 `docker compose up -d postgres` |
| 启动即退出，`CORS 配置无效` | `CORS_ALLOW_CREDENTIALS=true` 而 `CORS_ALLOWED_ORIGINS` 含 `*` | `grep -n "CORS 配置无效" 日志` | 改成具体来源，如 `https://app.example.com` |
| 启动日志有 `API Key 解密失败（AUTH_SECRET 可能已更换）` | 换了 `AUTH_SECRET`，库里旧密文解不开 | `curl -s localhost:8000/health/ready`，看 `provider_keys` 项 | 到管理后台重新填写各提供商的 API Key；旧明文行不受影响 |
| 启动日志有 `MCP 工具发现失败` 或 `部分 MCP server 不可用` | `MCP_SERVERS` 里某个 server 连不上 | `grep -n "MCP" 日志`；`curl -s localhost:8000/api/capabilities -b cookie.txt` 看 `mcp_error`（响应字段，不是 `discovery_error`） | 修 server 地址或从 `MCP_SERVERS` 里删掉该条。内置工具照常可用，不影响启动 |

## 认证、权限与跨域

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| `/api/auth/me` 返回 401 | 没有有效登录态（会话 cookie 缺失或过期） | `curl -i -s -X POST localhost:8000/api/auth/login -d 'username=a@example.com&password=...'` 看有没有 `Set-Cookie`；`curl -i -s localhost:8000/api/auth/me -b cookie.txt` | 重新登录。注意登录必须发**表单编码**，发 JSON 会 422 |
| 登录返回 204，但下一个请求 401 | cookie 被浏览器丢弃：`SameSite=None` 没配 `Secure`；或 `AUTH_COOKIE_SECURE=true` 而本地是 http | 浏览器 devtools 看 cookie 的 `SameSite` 与 `Secure` 列 | 本地 http 调试：把 `CORS_ALLOW_CREDENTIALS` 设为 `false`（SameSite 自动回落 `lax`）或显式 `AUTH_COOKIE_SAMESITE=lax`，再设 `AUTH_COOKIE_SECURE=false`；跨站部署则开 HTTPS 并设 `AUTH_COOKIE_SECURE=true` + `CORS_ALLOW_CREDENTIALS=true` |
| 管理后台接口返回 403，提示 `需要管理员权限` | 登录了但角色不够（`role >= admin`） | `curl -s localhost:8000/api/auth/me -b cookie.txt`，看 `role` 字段 | `uv run python scripts/seed_admin.py --email admin@example.com --promote`；或由 superadmin 在后台改角色 |
| 状态变更请求返回 403，提示 `跨站请求被拒绝` | `OriginGuardMiddleware` 判定为跨站：Origin 不在 `CORS_ALLOWED_ORIGINS` 里，且与 Host 不同 | 响应体是 `{"detail": "跨站请求被拒绝"}`（这个中间件不打日志）；浏览器 devtools 看请求的 `Origin` 头 | 把前端来源加进 `CORS_ALLOWED_ORIGINS`（逗号分隔）；前后端同源部署时 Host 相同会直接放行 |
| 前端报 CORS 错误，响应里没有 `Access-Control-Allow-Origin` | 后端未挂 CORS 头，或来源不在白名单 | `curl -i -s -X OPTIONS localhost:8000/api/auth/login -H 'Origin: https://app.example.com' -H 'Access-Control-Request-Method: POST'` | 配 `CORS_ALLOWED_ORIGINS`；跨站带 cookie 还要 `CORS_ALLOW_CREDENTIALS=true` 且来源不能是 `*` |
| 登录后每个请求都 401，但 curl 手工带 cookie 是好的 | 浏览器没带 cookie：`SameSite=Lax` 下跨站 fetch 不回传 | devtools 看请求头有没有 `Cookie` | 显式设 `AUTH_COOKIE_SAMESITE=none` 并配 `AUTH_COOKIE_SECURE=true`，或改成同源部署 |

## 模型与对话

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| 对话返回 503 `管理员尚未启用任何模型` | 后台一个启用模型都没有（不会回退到 `MODEL_NAME`） | `curl -s localhost:8000/api/models -b cookie.txt`，列表为空就是这个原因；这个 503 只进响应体，不进日志 | 在管理后台启用模型并设默认 |
| 对话报 `服务暂时不可用，请重试` | 上游 5xx 或超时。`MODEL_UPSTREAM_TIMEOUT` 默认 120 秒，是**空闲**超时不是总时长 | SSE 文案末尾带错误编号；`grep -n "流式生成异常\|上游瞬时失败" 日志` 按 `error_id` 对上 | 看上游网关本身；调 `MODEL_UPSTREAM_TIMEOUT` 或换模型 |
| 对话报 `上游认证配置问题` | 上游 401/403：key 错、没发 Authorization、或换了 `AUTH_SECRET` | `curl -s localhost:8000/health/ready` 看 `provider_keys`；管理后台看该提供商是否显示「未设置密钥」 | 重填 API Key；确认该模型在库中处于启用状态 |
| 备用模型不触发，日志有 `备用模型已配置但本轮无法启用` | 配了 `FALLBACK_MODEL_NAME`，但库里没有同名且启用的模型，或与主模型同名 | `grep -n "备用模型已配置但本轮无法启用" 日志`，信息里有具体 reason | 在管理后台添加同名模型并启用；确认 `FALLBACK_MODEL_NAME` 与模型 id 精确一致（大小写敏感） |
| 备用模型不触发，且没有上面那条日志 | 错误类型不在可切换范围：401/403 与 400/404 不切（同一套凭据下备用也会失败） | 看 SSE 的 `error` 事件文案 | 修凭据或请求本身；换 5xx/429/超时类故障才能验证容灾 |
| 对话报 `上游限流，请稍后重试` | 上游返回 429（不是本服务自己的限流） | 日志里的 `模型调用失败，尝试备用模型`；`model_fallbacks_total` 是否同时增长；SSE `fallback` 事件带 `reason=上游限流` | 稍后重试或换模型；这类错误会切备用模型（如果配了） |
| SSE 输出一次性全到，没有逐字效果 | 中间层缓冲了响应 | 响应头里应能看到 `X-Accel-Buffering: no`；`curl -N -s -X POST localhost:8000/api/chat ...` | Nginx 侧关 `proxy_buffering`；别用会缓冲的 CDN / 网关 |
| SSE 传到一半断开 | 半开连接下单次 ASGI send 阻塞超过 `SSE_SEND_TIMEOUT_SECONDS = 30` | 服务端没有专门的日志行（超时发生在响应写出阶段），按断开时刻与反代读超时对时间 | 调大反代读超时；检查上游是否长时间无数据 |

## 沙箱

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| 模型回复 `当前执行环境资源已满，请稍后再试。` | 全局并发到 `SANDBOX_MAX_CONCURRENT`（默认 12），或该用户已占用 `SANDBOX_MAX_PER_USER`（默认 1） | `grep -n "沙箱命令执行失败" 日志`，类型名是 `SandboxCapacityError` | 等回收；或临时调大上限。容量保护不排队，超限直接拒绝。日志只记类型名：区分「该用户已满」与「全局池满」要数 `sandbox_lease` 里未结束的行 |
| 租约卡在 `destroying`，机器没被拆 | 回收条件 UPDATE 抢占后进程崩了；`destroying` 超过 `DESTROYING_STALE_SECONDS` 才允许重抢 | 查 `sandbox_lease` 表 `state='destroying'` 的行与 `destroying_since` | 确认回收循环在跑（`grep -n "沙箱回收一轮" 日志`）；等重试门槛到期，或手工把状态改回 `destroy_failed` 让回收接手 |
| 回收循环不工作 | 回收任务没起：它跟 API 共用同一个事件循环，`main.py` 的 `_run_all` 一起拉起 | `grep -n "沙箱回收循环启动" 日志`；`grep -n "沙箱回收一轮" 日志` | 用 `python main.py` 启动（不要单独跑 uvicorn）；检查是否只有 API 进程没有回收进程 |
| 日志有 `回收：租约卡在 provisioning` | 建机过程未完成，控制面可能中途崩溃 | 查 `sandbox_lease` 表 `state='provisioning'` 且创建时间很早的行 | 该租约已被标记失败待人工核对；需人工确认 provider 侧是否留有机器 |
| 日志有 `回收：销毁失败` | provider 拒绝销毁 | `grep -n "回收：销毁失败" 日志`，后面带 lease id | 查 OpenSandbox server 是否可达（`SANDBOX_SERVER_URL`）；失败不是终态，下一轮会重试 |
| 心跳不生效，机器被提前回收 | 心跳写 Redis 失败，或用户页面已关闭 | `grep -n "沙箱心跳写入失败" 日志`；回收侧看到的是 `回收：读取心跳失败，跳过` | 修 Redis 连通性；注意心跳只证明「客户端还在」，活动（`last_activity`）证明机器还在被用 |

## 同步

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| 同步 push 返回 400，`code=too_many_rows` | 单次 push 超过 `SYNC_PUSH_MAX_ROWS`（默认 200 行，会话 + 消息合计） | 响应体 `detail` 末尾带 `（code=too_many_rows）`；服务端不为此打日志 | 客户端分批推 |
| 同步 pull 返回 400，`code=invalid_cursor` | 游标格式不对或为负 | 响应体 `detail` 末尾带 `（code=invalid_cursor）` | 客户端丢弃游标，重拉全量 |
| 同步 pull 返回 410，`code=resync_required` | 游标版本超过服务端当前版本（服务端数据被重置过） | 响应体 `detail` 末尾带 `（code=resync_required）` | 客户端丢弃游标全量重拉。410 与 400 对客户端动作相同：丢弃游标重拉 |
| 同步迟迟不更新，但没报错 | 长轮询是**单进程**唤醒：多进程时 A 进程的写入唤不醒 B 进程的等待方 | 确认部署是不是多进程；看 `SYNC_LONG_POLL_SECONDS`（默认 25 秒） | 单进程部署，或把唤醒换成 Redis pub/sub；否则只能等挂起超时 |
| 同步 push 返回 413 `请求体过大，已拒绝` | 整批 body 超过 `MAX_REQUEST_BODY_BYTES`（默认 32 MB），`MaxBodySizeMiddleware` 在读完之前就拒 | 响应体是 `{"detail": "请求体过大，已拒绝"}`；这个中间件不打日志 | 客户端减小批次；单条 `payload` 上限是 `SYNC_PAYLOAD_MAX_BYTES`（默认 256 KB） |
| 同步请求 429 | 同步桶按账号限流，`SYNC_RATE_LIMIT_PER_MINUTE`（默认 120，push + pull 共用） | 响应头 `Retry-After` | 客户端按 `Retry-After` 退避 |

## 限流与依赖

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| 收到 429，响应头有 `Retry-After` | 触发限流。对话按账号（`CHAT_RATE_LIMIT_PER_MINUTE`，默认 30）、认证按来源 IP（默认 10）、同步单独一桶（默认 120）、改密单独一桶（默认 5） | `curl -i -s ...` 看 `Retry-After` 与 `X-RateLimit-Remaining` | 退避重试；配额是每分钟窗口。`0` 表示不限流 |
| 所有用户被算成同一个来源，登录全被限流 | 部署在反代之后但没配 `TRUSTED_PROXY_IPS` | 启动日志有 `认证限流按 client.host 计数` | 同时配 `TRUSTED_PROXY_HEADERS` 与 `TRUSTED_PROXY_IPS`；只配标头不配网段等于「谁来都信」，限流器会 fail closed 不采信标头 |
| 限流配额像是被放大 | Redis 不可用时降级成进程内计数，配额乘以实例数 | 日志有 `Redis 限流不可用，降级模式=`；`redis_unavailable_total` 增长 | 修 Redis；或改 `RATE_LIMIT_ON_REDIS_ERROR` 为 `fail_closed`（牺牲可用性换确定性）/ `fail_open` |
| `/health/ready` 返回 503，`status=not_ready` | 必需依赖失败，默认只有 `database` | `curl -s localhost:8000/health/ready \| python -m json.tool`，看 `database.reason`（`timeout` / `unavailable`）；`grep -n "readiness 检查失败" 日志` | 查 Postgres 是否在跑；连接串必须是 `postgresql+asyncpg://` |
| `/health/ready` 里 `redis` 是 `unavailable`，但整体仍是 200 | Redis 挂了只降级：限流变进程内、会话回源库、搜索缓存 miss | `curl -s localhost:8000/health/ready`，看 `status=degraded` 与 `redis.reason` | 修 Redis。降级会在日志与指标里显式出现，但不该让整个实例被摘流 |
| `/health/ready` 里 `storage` 是 `unavailable`，`reason=not_configured` | 开了沙箱但对象存储没配好 | `curl -s localhost:8000/health/ready`；启动日志有 `对象存储未配置，沙箱产物将不会被导出` | 配 `S3_ENDPOINT_URL` / 凭据 / 桶名；桶名必须是小写 DNS 名。自建网关用 path-style（`S3_USE_PATH_STYLE=true`） |
| `/api/metrics` 返回 404 | `METRICS_ENABLED=false`（默认） | `curl -o /dev/null -w '%{http_code}' localhost:8000/api/metrics` | 设 `METRICS_ENABLED=true`，并把该端点挡在内网或反代白名单后面 |

## 前端与文档站

| 症状 | 原因 | 验证 | 处理 |
|---|---|---|---|
| 前端页面空白，控制台报 404 | Vite dev server 没起，或 `/api` 代理打错后端端口 | `cd frontend && npm run dev`；devtools 网络面板看请求 URL | 代理目标是 `http://127.0.0.1:8000`；确认后端在跑 |
| 前端 401 循环，登录后立刻被登出 | 同源代理没生效，前端直连了别的地址 | devtools 看请求是发到 5173 还是 8000 | 确认 `vite.config.js` 的 `server.proxy` 仍配着 `/api`；生产用反代把 `/api` 与静态资源收到同一域名 |
| `npm run build` 报类型错误 | TypeScript 编译不过 | `cd frontend && npm run typecheck` | 修类型；`npm run build` 会先跑 `tsc --noEmit`，所以类型错误会挡住构建 |
| 文档站构建失败 | 死链、frontmatter 缺失或图片路径不对 | `cd docs && npm run build` | 按报错里的路径修；站内链接必须用相对 Markdown 路径（如 `../ops/testing.md`），且每页都要有 `title` 与 `description` 的 frontmatter |
| 文档站本地 base 与线上不一致 | 本地 `VITEPRESS_BASE=/`，线上 `/bennett-agent/` | `cd docs && npm run dev` | 用 `npm run dev` / `npm run build` 脚本，别绕过 `VITEPRESS_BASE` |

## 通用排查动作

| 动作 | 命令 |
|---|---|
| 看关联 ID | 响应头 `X-Request-ID`；日志行的 `[request_id]` 字段 |
| 看当前依赖状态 | `curl -s localhost:8000/health/ready \| python -m json.tool` |
| 看指标 | `curl -s localhost:8000/api/metrics`（需 `METRICS_ENABLED=true`） |
| 看回收循环 | `grep -n "沙箱回收" 日志` |
| 看降级与告警 | `grep -n "降级\|不可用\|失败" 日志` |
| 看限流配置是否生效 | `grep -n "认证限流按 client.host 计数" 日志` |
| 看审计流水 | `tail -n 20 quality/runs/core-behavior/<run_id>/audit.jsonl` |

## 相关源码

- `app/api/ops.py`：探针与指标端点
- `app/server.py`：启动自检、错误处理与中间件注册顺序
- `app/config/settings.py`：全部配置项与生产环境的启动时校验
- `app/api/rate_limit.py`：限流桶、降级模式与可信代理判定
- `app/services/upstream_errors.py`：上游错误的分类与可切换判定
- `app/services/sandbox/reaper.py` 与 `app/services/sandbox/lease.py`：回收与租约状态机
- `app/services/sync_service.py` 与 `app/services/sync_notifier.py`：游标语义与长轮询唤醒
- `app/middleware.py`：请求体上限、跨站守卫与安全响应头

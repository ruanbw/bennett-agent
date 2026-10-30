---
title: 配置手册
description: 按配置组说明每个变量的作用、边界与启动期守卫，并给出 dotenv 加载优先级。
---

# 配置手册

配置全部来自环境变量与 dotenv 文件，由 `app/config/settings.py` 的 `Settings` 统一加载与校验。本页按组讲「这个变量管什么、什么时候必须改、改错了会怎样」，不逐条抄 `.env.example`（那份文件自带逐项中文注释，是配置的权威清单）。

三条贯穿全篇的规则：

1. **配置在进程启动时读一次**。改了 `.env` 要重启进程；开发期 `--reload` 会自动重启。
2. **生产守卫挂在 `APP_ENV=production` 上**。环境名拼错会被拒绝，但写成 `Production` 会被归一化成 `production`——归一化先于校验，所以大小写不影响守卫。
3. **数据库是权威**。模型提供商、模型清单、启停状态都在库里，环境变量只在首次启动时做一次引导。

## APP_ENV 与 dotenv 加载优先级

`APP_ENV` 只接受 `development` / `production` / `staging` / `test`（大小写、首尾空格会被归一化）。它决定额外加载哪个 `.env.{APP_ENV}` 文件，也决定所有生产守卫是否生效。

环境名本身按「系统环境变量 > `.env.local` > `.env` > `development`」解析——必须先知道环境名才能决定加载哪些文件，否则 `.env` 里的 `APP_ENV` 会被自己忽略。

加载的文件顺序（`get_env_files()`）：

```text
.env  →  .env.local  →  .env.{APP_ENV}  →  .env.{APP_ENV}.local
```

**靠后的覆盖靠前的**，而系统环境变量始终最高。生效优先级从高到低：

| 优先级 | 来源 |
|---|---|
| 1 | 系统环境变量（`export AUTH_SECRET=...`） |
| 2 | `.env.{APP_ENV}.local` |
| 3 | `.env.{APP_ENV}` |
| 4 | `.env.local` |
| 5 | `.env` |

只存在的文件参与加载，缺失的文件跳过。`.gitignore` 忽略全部 `.env*`，四个文件都可以安全地放本地覆盖。

实际用途：`APP_ENV=staging` 时放一个 `.env.staging`，里面只写与生产不同的项（网关地址、模型名），其余仍从 `.env` 继承。

## 模型网关与备用模型

| 变量 | 作用 |
|---|---|
| `LITELLM_API_BASE` | OpenAI 兼容端点的基础地址 |
| `LITELLM_API_KEY` | 网关密钥 |
| `MODEL_NAME` | 默认模型，LiteLLM 路由格式（`openai/gpt-4o`、`auto/deepseek-v4.1-flash`） |
| `FALLBACK_MODEL_NAME` | 备用模型，留空 = 不启用 |
| `MODEL_DESCRIPTION_FIELD` | 上游模型条目里用作展示描述的字段名，缺失时回退为「provider name」 |
| `MODEL_UPSTREAM_TIMEOUT` | 上游**空闲**超时（秒），0 = 不限制 |
| `PROVIDER_BLOCK_PRIVATE_TARGETS` | 提供商出站目标是否必须是公网地址 |
| `BOOTSTRAP_PROVIDER_FROM_ENV` | 首次启动是否用上面的三项引导一条「默认网关」 |

三个容易踩的点：

- **主模型失败不是都切备用**。5xx / 429 / 超时 / 连接失败才切；401、403、400、404 不切（同一套凭据或请求本身的问题，换模型救不回来）。流式回答只在「还没输出任何内容」时切换，不会重复输出。
- **备用模型必须接受与主模型相同的调用参数**（`reasoning_effort`、超时），否则切换会以 400 失败。
- **引导只写库、不联网**，且只在「从未初始化过」时发生一次。删光提供商不会回退到环境变量——那会得到「没有任何可用模型」。要停用某个提供商请在后台禁用它，而不是删除。

超时是空闲超时而非总时长，所以不会提前掐断长长的流式输出。

## 上下文预算

历史由客户端全量回放上来，字符数上限挡不住 token 溢出。每轮生成前按网关报的 `context_window` 粗估用量（历史 + 系统提示词 + 工具 schema），再按模式处置。

| 变量 | 取值 | 作用 |
|---|---|---|
| `CONTEXT_GUARD_MODE` | `compact`（默认）/ `reject` / `off` | 超限处置方式 |
| `CONTEXT_RESERVE_TOKENS` | 默认 8192 | 预留 token（网关报了 `max_output_tokens` 就用它） |
| `CONTEXT_SAFETY_RATIO` | 默认 0.9 | 可用上限 =（窗口 − 预留 − 固定开销）× 该值 |
| `CONTEXT_COMPACT_KEEP_RECENT_TURNS` | 默认 4 | 压缩时保留最近多少轮问答原文 |
| `CONTEXT_COMPACT_MESSAGE_MAX_CHARS` | 默认 4000 | 压缩第一级：单条字符上限 |

`compact` 的两级策略：先截断超长单条的中间部分（纯计算，不调模型，保留首尾），再把更早的轮次折叠成一份事实清单注入本轮 instructions（会调一次模型）。压缩改变了模型看到的上下文，所以每次压缩都下发 `compaction` 事件，前端显示成一行提示。

`reject` 会把用户的路走死（消息已落库，下一轮只会更长），`off` 适合你确定上游不会超限的场合。网关没报 `context_window` 的模型一律放行：拿不到数据就不猜。

## 联网搜索与 MCP

| 变量 | 作用 |
|---|---|
| `TAVILY_API_KEY` | 搜索工具的 API Key |
| `MCP_SERVERS` | MCP server 声明，分号分隔 |
| `MCP_TOOL_TIMEOUT_SECONDS` | 单个 MCP 工具调用的超时（秒），默认 30 |

`MCP_SERVERS` 每条形如 `名称=传输:目标`：

```text
filesystem=url:https://mcp.example.com/mcp
local=stdio:/usr/local/bin/my-mcp-server --port 8080
```

名称只能用字母/数字/下划线/短横线（它会变成工具名前缀，如 `filesystem__read_file`），且必须唯一。工具是**动态发现**的：进程启动时连一次，server 报什么工具就挂什么，代码不用改。写错的声明会让启动失败而不是被静默跳过——声明出来的能力不能悄悄消失。

只支持 http/https 与本地可执行文件：`file://` 之类能读本地文件，不该由「加个 server」开启。

## 数据库、Redis 与对象存储

| 组 | 关键变量 |
|---|---|
| 数据库 | `DATABASE_URL`、`DATABASE_POOL_SIZE`、`DATABASE_MAX_OVERFLOW`、`DATABASE_POOL_TIMEOUT`、`DATABASE_POOL_RECYCLE`、`DATABASE_STATEMENT_TIMEOUT_MS`、`DATABASE_APPLY_SCHEMA` |
| Redis | `REDIS_URL`、`REDIS_KEY_PREFIX`、`REDIS_SESSION_CACHE_ENABLED`、`SEARCH_CACHE_BACKEND`、`READINESS_REQUIRED_DEPENDENCIES` |
| 对象存储 | `S3_ENDPOINT_URL`、`S3_REGION`、`S3_ACCESS_KEY`、`S3_SECRET_KEY`、`S3_BUCKET`、`S3_PREFIX`、`S3_USE_PATH_STYLE`、`S3_CREATE_BUCKET`、`S3_PRESIGN_EXPIRY_SECONDS` |

几条硬约束：

- **`DATABASE_URL` 只接受 `postgresql+asyncpg://`**。SQLite 已被移出支持矩阵：残留的 sqlite 串不会报错，而是静默建出一个空库，所以配置层直接拒绝。
- **总连接数 =（`POOL_SIZE` + `MAX_OVERFLOW`）× worker 数**，必须明显小于 Postgres 的 `max_connections`。compose 里已经把 Postgres 调到 200。
- **`DATABASE_POOL_RECYCLE` 必须短于 Postgres / pgbouncer / 云负载均衡的空闲断连时间**，否则会拿到一批已经死掉的连接。
- **Redis 不是可用性单点**。每处使用都有降级路径（限流退进程内计数、缓存 miss、会话回源数据库），但降级会在日志与 `/health/ready` 里体现。`REDIS_KEY_PREFIX` 在多套部署共用一个 Redis 时必须各不相同。
- **对象存储只服务沙箱产物与上传原件**。`S3_SECRET_KEY` 生产必须由密钥管理注入，不写进镜像或 compose 文件。桶名必须是小写 DNS 名（带大写或下划线会在建桶时被 S3 直接拒绝）。
- **`S3_CREATE_BUCKET=true` 是开发便利**：生产建议关掉，由 IaC 建桶并只授予最小权限。

## 认证、会话与 Cookie

| 变量 | 作用 |
|---|---|
| `AUTH_SECRET` | 令牌签名密钥 + API Key 加密密钥 |
| `AUTH_COOKIE_NAME` | 会话 cookie 名，默认 `bennett_session` |
| `AUTH_COOKIE_SECURE` | cookie 是否只走 HTTPS |
| `AUTH_COOKIE_SAMESITE` | `lax` / `strict` / `none`，留空 = 自动 |
| `SESSION_LIFETIME_SECONDS` | 登录态有效期，默认 30 天 |
| `REGISTRATION_ENABLED` | 是否允许公开注册，默认 true |

会话走 HttpOnly cookie + 服务端会话表（不用 JWT）。`AUTH_COOKIE_NAME` 改名等于让所有已登录用户掉线，多实例部署必须保持一致。

`SameSite` 留空时自动：凭据模式（`CORS_ALLOW_CREDENTIALS=true`）取 `none`，否则取 `lax`。显式取 `none` 但没开 `AUTH_COOKIE_SECURE` 会**启动即报错**——浏览器会直接丢弃「SameSite=None 但没有 Secure」的 cookie，表现为「登录接口 204、下一个请求 401」。同理，cookie 名带 `__Host-` / `__Secure-` 前缀时也强制要求 `AUTH_COOKIE_SECURE=true`。

### 三条必须记住的规则

**弱 `AUTH_SECRET` 拒绝启动。** 生产环境下 `AUTH_SECRET` 若是默认占位值、含占位词（`change-me` / `replace-with` / `your-` / `example` / `placeholder` / `insecure`），或短于 32 字符，进程直接启动失败，不打警告继续跑。理由很直接：任何人都能用公开的默认密钥伪造重置密码令牌，任意账号可被接管。生成方式：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

**`AUTH_SECRET` 兼作 API Key 加密密钥。** 它经 SHA-256 派生出 Fernet 密钥，用来加密数据库里的提供商 API Key（存储为 `enc:v1:<token>`，读取时透明解密）。代价是：**更换 `AUTH_SECRET` 后，已加密的 API Key 解不开**，需要在管理后台重新填写。升级前写入的明文行不受影响，仍按原样读取，并在下次保存时自动加密。启动日志与 `/health/ready` 会明确提示「有 N 个提供商的 API Key 解密失败」。

**占位 Key 视为未配置。** `TAVILY_API_KEY=tvly-your-api-key` 这类值不算配置成功：搜索工具不会被装配，空对话页也不会推荐搜索类起步示例，能力探针不会谎报「可联网」。识别规则是匹配 `your-api-key` / `your-secret` / `change-me` / `replace-me` / `placeholder` / `example` 这类占位词，不猜格式或长度——各上游 key 形状不同，多加校验只会误杀合法密钥。

## 限流与可信代理

| 变量 | 作用 |
|---|---|
| `CHAT_RATE_LIMIT_PER_MINUTE` | 单账号每分钟的对话/标题请求数，默认 30 |
| `AUTH_RATE_LIMIT_PER_MINUTE` | 单来源 IP 每分钟的登录/注册数，默认 10 |
| `CHANGE_PASSWORD_RATE_LIMIT_PER_MINUTE` | 单账号每分钟的改密请求数，默认 5 |
| `SYNC_RATE_LIMIT_PER_MINUTE` | 单账号每分钟的同步请求数，默认 120 |
| `RATE_LIMIT_BACKEND` | `redis`（生产默认）/ `memory` |
| `RATE_LIMIT_ON_REDIS_ERROR` | `local_fallback`（默认）/ `fail_closed` / `fail_open` |
| `TRUSTED_PROXY_HEADERS` | 受信任代理的 IP 标头，如 `X-Forwarded-For,X-Real-IP` |
| `TRUSTED_PROXY_IPS` | 受信任代理的 IP/CIDR 列表 |

改密限流不是可选项：每次验证当前密码是一次约 65ms 的 Argon2 计算，不限流时任意注册用户都能用它打满 CPU，被盗会话也能借此高速爆破当前密码（绕过登录端点的 IP 限流）。

**只配标头名而不配 `TRUSTED_PROXY_IPS` 等于让任何直连客户端自己填 `X-Forwarded-For` 来换限流桶**（每次换一个值就绕过 10 次/分钟）。两者都配后，只有直连对端落在受信网段内才采信标头，并从右往左跳过受信代理、取第一个非代理地址。

认证限流按来源 IP 计数，没有账号身份可用。反向代理部署时不配这两项，所有请求会被算作同一个来源（全站共享一个桶），启动日志会就此告警。

## 沙箱

| 变量 | 作用 |
|---|---|
| `SANDBOX_ENABLED` | 总开关，默认 false |
| `SANDBOX_PROVIDER` | 目前只有 `opensandbox` |
| `SANDBOX_SERVER_URL` / `SANDBOX_SERVER_API_KEY` | server 地址与密钥 |
| `SANDBOX_TEMPLATE` / `SANDBOX_TEMPLATE_VERSION` | 模板镜像名与版本（版本号落库到租约上，便于追溯「用户当时跑在哪一版」） |
| `SANDBOX_CPU` / `SANDBOX_MEMORY_MB` / `SANDBOX_WORKDIR` | 每台机器的资源配额 |
| `SANDBOX_IDLE_SECONDS` / `SANDBOX_HARD_TTL_SECONDS` / `SANDBOX_HEARTBEAT_SECONDS` | 生命周期 |
| `SANDBOX_MAX_PER_USER` / `SANDBOX_MAX_CONCURRENT` | 容量保护 |
| `SANDBOX_EGRESS` | `proxy`（生产默认，白名单代理）/ `open`（自由联网，启动会打警告） |
| `SANDBOX_ARTIFACT_MAX_BYTES` | 销毁前导出产物的体积上限 |

默认全关：没有 sandbox 服务的部署不该凭空多出四个工具。关闭时能力探针报 `sandbox=false`，模型看不到这些工具。

`SANDBOX_MAX_PER_USER` 是容量保护，不是付费分级——超限返回 429，不静默排队。`SANDBOX_EGRESS=open` 只给本地/自用：多租户下自由联网等于把「上传件可能被外带」交给模型决定。

## 出站 SSRF 防护

`PROVIDER_BLOCK_PRIVATE_TARGETS` 控制「提供商出站目标是否必须是公网地址」。

- 默认 `false`：本机 LiteLLM 网关（默认地址就是 `http://localhost:4000`）与内网自建 vLLM 都需要它，开了会把它们拦掉。
- 设为 `true` 后，环回/私有/保留段一律拒绝。
- **无论开关如何，链路本地、未指定、组播地址永远被拒绝**——云厂商元数据服务 `169.254.169.254` 就在这一段里。

只在服务对外暴露、需要把「管理员填错地址」的风险压到最低时开启。

## 指标、API 文档与日志

| 变量 | 作用 |
|---|---|
| `METRICS_ENABLED` | 是否暴露 `/api/metrics`（Prometheus 文本格式），默认 false |
| `ENABLE_API_DOCS` | 是否暴露 `/docs`、`/redoc`、`/openapi.json` |
| `LOG_LEVEL` | `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`，配错退回 INFO |
| `LOG_ACCESS` | 是否输出 uvicorn 访问日志，默认关闭 |
| `ASSISTANT_NAME` | 展示给用户的助手名称 |
| `MAX_REQUEST_BODY_BYTES` | 单个 HTTP 请求体硬上限，默认 32MB |

`ENABLE_API_DOCS` 留空 = 自动：`APP_ENV=production` 时关闭，其余环境开启。文档页把端点结构白送给未认证访客，生产默认关掉；确实需要时再显式打开，并挡在内网或反代白名单后面。

`METRICS_ENABLED` 默认关闭同理：它不需要登录就能访问，多开一个端点就多一分侦察面。关闭时端点返回 **404** 而不是 403——404 比「存在但你不能看」泄露得更少。

`LOG_LEVEL=DEBUG` 下第三方库（httpx / openai 等）仍被压在 WARNING：它们会把请求头打进日志，里面就是 Authorization 与 API Key。访问日志默认关闭，因为 SSE 长连接与前端轮询会让它变成噪音，真正要看的 WARNING 会被淹没。

`MAX_REQUEST_BODY_BYTES` 是一道必要的闸：Starlette/uvicorn 默认不限制请求体，同步接口的单条 payload 校验发生在 JSON 完整解析之后，没有这道闸时任意已登录用户（注册开放）都能用超大 body 把进程内存打满。反向代理上建议再配一层更小的值。

## 相关源码

- `app/config/settings.py`
- `app/config/secret_box.py`
- `app/config/proxy_env.py`
- `app/server.py`
- `app/api/rate_limit.py`
- `app/services/context_compaction.py`
- `app/services/token_budget.py`
- `.env.example`

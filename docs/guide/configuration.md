---
title: 配置手册
description: 按配置组说明每个变量的作用、默认值与启动期守卫，并给出 dotenv 加载优先级。
---

# 配置手册

配置全部来自环境变量与 dotenv 文件，由 `app/config/settings.py` 的 `Settings` 统一加载与校验（89 个字段）。本页按组讲「这个变量管什么、默认值是多少、改错了会怎样」，不逐条抄 `.env.example`——那份文件自带逐项中文注释，是同一份配置的另一个副本，两处对不上时以本页为准（本页对齐的是代码）。

三条贯穿全篇的规则：

1. **配置在进程启动时读一次**。改了 `.env` 要重启进程。开发期 `uv run python main.py --reload` 会监听 `app/`、`prompts/`、`main.py` 与实际被加载的 dotenv 文件，保存即重启（`APP_ENV=production` 下 `--reload` 被直接拒绝）。
2. **生产守卫挂在 `APP_ENV=production` 上**。环境名拼错会被拒绝，但写成 `Production` 会被归一化成 `production`——归一化先于校验，所以大小写不影响守卫。
3. **数据库是权威**。模型提供商、模型清单、启停状态都在库里，环境变量只在首次启动时做一次引导。

逐条的守卫清单在文末「启动期守卫」，这里不重复。

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

只存在的文件参与加载，缺失的文件跳过。仓库根的 `.gitignore` 用 `.env*` 忽略全部环境文件，只放行 `.env.example` 与 `frontend/.env` 两个不含密钥的模板——所以 `.env.local`、`.env.staging` 都可以安全地放本地覆盖。

实际用途：`APP_ENV=staging` 时放一个 `.env.staging`，里面只写与生产不同的项（网关地址、模型名），其余仍从 `.env` 继承。

例外：`LOG_LEVEL` 与 `LOG_ACCESS` 不在 `Settings` 里（见文末「指标、API 文档与日志」），不参与上面这套文件加载。

## 模型网关与备用模型

| 变量 | 默认 | 作用 |
|---|---|---|
| `LITELLM_API_BASE` | `http://localhost:4000` | OpenAI 兼容端点的基础地址 |
| `LITELLM_API_KEY` | 空 | 网关密钥 |
| `MODEL_NAME` | `openai/gpt-4o` | 默认模型，LiteLLM 路由格式 |
| `FALLBACK_MODEL_NAME` | 空 | 备用模型，留空 = 不启用；与主模型同名也视为不启用 |
| `MODEL_DESCRIPTION_FIELD` | `magpie_label` | 上游模型条目里用作展示描述的字段名 |
| `MODEL_UPSTREAM_TIMEOUT` | `120` | 上游**空闲**超时（秒），0 = 不限制 |
| `PROVIDER_BLOCK_PRIVATE_TARGETS` | `false` | 提供商出站目标是否必须是公网地址 |
| `BOOTSTRAP_PROVIDER_FROM_ENV` | `true` | 首次启动是否用上面三项引导一条「默认网关」 |

`MODEL_DESCRIPTION_FIELD` 是一条回退链，不是单一字段：先取配置的字段名，取不到用上游的 `description`，再取不到拼成「`owned_by` + 展示名」（`app/services/model_service/upstream.py`）。做成配置项是因为展示标签的字段名属于网关细节，硬编码在这里会让一个自称网关无关的模块耦合到特定网关。

三个容易踩的点：

- **主模型失败不是都切备用**。5xx / 429 / 超时 / 连接失败才切；401、403、400、404 不切（同一套凭据或请求本身的问题，换模型救不回来）。判定只有一份实现：`app/services/upstream_errors.py:is_fallback_eligible`。
- **备用模型必须接受与主模型相同的调用参数**（`reasoning_effort`、超时），否则切换会以 400 失败。流式回答只在「还没输出任何内容」时切换（`FallbackModel` 只在流打开阶段接管），不会重复输出。
- **引导只写库、不联网**，且只在「从未初始化过」时发生一次。删光提供商不会回退到环境变量——那会得到「没有任何可用模型」。要停用某个提供商请在后台禁用它，而不是删除。

超时是空闲超时而非总时长，所以不会提前掐断长长的流式输出。

## 上下文预算

历史由客户端全量回放上来，字符数上限挡不住 token 溢出。每轮生成前按网关报的 `context_window` 粗估用量（历史 + 系统提示词 + 工具 schema），再按模式处置。

| 变量 | 默认 | 作用 |
|---|---|---|
| `CONTEXT_GUARD_MODE` | `compact` | `compact` / `reject` / `off`，超限处置方式 |
| `CONTEXT_RESERVE_TOKENS` | `8192` | 预留 token；网关报了 `max_output_tokens` 就用它 |
| `CONTEXT_SAFETY_RATIO` | `0.9` | 可用上限 =（窗口 − 预留 − 固定开销）× 该值，取值 `(0, 1]` |
| `CONTEXT_COMPACT_KEEP_RECENT_TURNS` | `4` | 压缩时保留最近多少轮问答原文 |
| `CONTEXT_COMPACT_MESSAGE_MAX_CHARS` | `4000` | 压缩第一级：单条字符上限，下限 500 |

预留值不管来自配置还是上游声明，都会被再压一道「窗口的一半」以内，避免窗口小的模型被预留吃光预算（`app/services/token_budget.py:resolve_reserve_tokens`）。

`compact` 的两级策略：先截断超长单条的中间部分（纯计算，不调模型，保留首尾，绝不碰本轮问题），再把更早的轮次折叠成一份事实清单注入本轮 instructions（会调一次模型）。压缩改变了模型看到的上下文，所以每次压缩都下发 `compaction` 事件，前端显示成一行提示。

`reject` 会把用户的路走死（消息已落库，下一轮只会更长），`off` 适合你确定上游不会超限的场合。网关没报 `context_window` 的模型一律放行：拿不到数据就不猜。

## 联网搜索与 MCP

| 变量 | 默认 | 作用 |
|---|---|---|
| `TAVILY_API_KEY` | 空 | 搜索工具的 API Key；占位值视为未配置（见「认证、会话与 Cookie」末段） |
| `MCP_SERVERS` | 空 | MCP server 声明，分号分隔 |
| `MCP_TOOL_TIMEOUT_SECONDS` | `30` | 单个 MCP 工具调用的超时（秒），取值 1–600 |

`MCP_SERVERS` 每条形如 `名称=传输:目标`：

```text
filesystem=url:https://mcp.example.com/mcp
local=stdio:/usr/local/bin/my-mcp-server --port 8080
```

名称只能用字母/数字/下划线/短横线，且不超过 32 字符（它会变成工具名前缀，如 `filesystem__read_file`），并且必须唯一。工具是**动态发现**的：进程启动时连一次，server 报什么工具就挂什么，代码不用改。写错的声明会让启动失败而不是被静默跳过——声明出来的能力不能悄悄消失（解析在 `app/tools/mcp_client.py:load_server_configs`）。

只支持 http/https 与本地可执行文件：`file://` 之类能读本地文件，不该由「加个 server」开启。工具发现失败本身不阻止启动，只记 WARNING 并把错误带进 `/api/capabilities` 的 `mcp_error`。

## 数据库

| 变量 | 默认 | 作用 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://bennett:bennett@127.0.0.1:5432/bennett` | 连接串，只接受 `postgresql+asyncpg://` |
| `DATABASE_POOL_SIZE` | `10` | 连接池常驻连接数，下限 1 |
| `DATABASE_MAX_OVERFLOW` | `10` | 池满后允许临时新建的连接数，0 = 不允许溢出 |
| `DATABASE_CONNECT_TIMEOUT` | `5` | 建连超时（秒） |
| `DATABASE_POOL_TIMEOUT` | `10` | 等空闲连接的最长秒数 |
| `DATABASE_POOL_RECYCLE` | `1800` | 连接最长存活秒数，0 = 不回收 |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `30000` | 服务端语句超时（毫秒），0 = 不限制 |
| `DATABASE_POOL_MODE` | `pool` | `pool` = 常驻连接池；`null` = 每次新建连接 |
| `DATABASE_APPLY_SCHEMA` | `true` | 启动时执行 `sql/init.sql` 并比对结构 |

几条硬约束：

- **只接受 `postgresql+asyncpg://`**。SQLite 已被移出支持矩阵：残留的 sqlite 串不会报错，而是静默建出一个空库，所以配置层直接拒绝。
- **总连接数 =（`POOL_SIZE` + `MAX_OVERFLOW`）× worker 数**，必须明显小于 Postgres 的 `max_connections`。`docker-compose.yml` 里已经把 Postgres 调到 200，注释里写了算式。
- **`DATABASE_POOL_RECYCLE` 必须短于 Postgres / pgbouncer / 云负载均衡的空闲断连时间**，否则会拿到一批已经死掉的连接。
- **`DATABASE_POOL_TIMEOUT` 要明显小于客户端与反代的超时**：等 30 秒再报错时，用户侧的连接早就断了，日志里只剩无意义的 500。
- **`DATABASE_POOL_MODE=null` 只给短命进程**（迁移、脚本）和同步 `TestClient`：池里的 asyncpg 连接绑定在创建它的事件循环上，跨循环复用会抛 `Future attached to a different loop`。
- **`DATABASE_APPLY_SCHEMA=false` 会同时跳过建表与结构比对**（`app/db/schema.py:verify_schema_at_head` 直接 return）。默认路径下脚本整段幂等，之后逐列比对模型与库，缺列就带着可复制的 `ALTER TABLE` 拒绝启动。

## Redis

| 变量 | 默认 | 作用 |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 连接串 |
| `REDIS_KEY_PREFIX` | `bennett` | 所有键的公共前缀 |
| `REDIS_CONNECT_TIMEOUT` | `2` | 建连超时（秒） |
| `REDIS_SOCKET_TIMEOUT` | `1` | 单次命令读写超时（秒） |
| `REDIS_MAX_CONNECTIONS` | `50` | 每进程连接池上限 |
| `REDIS_SESSION_CACHE_ENABLED` | `true` | 是否用 Redis 加速会话 token → 用户解析 |
| `REDIS_SESSION_CACHE_TTL_SECONDS` | `60` | 会话缓存最长存活秒数（同时不超过会话剩余寿命） |
| `SEARCH_CACHE_BACKEND` | `redis` | Tavily 搜索缓存：`redis` 共享 / `memory` 进程内 LRU |
| `READINESS_REQUIRED_DEPENDENCIES` | `database` | 就绪探针里必须可用的依赖，逗号分隔 |

- **Redis 不是可用性单点**。每处使用都有降级路径（限流退进程内计数、缓存 miss、会话回源数据库），但降级会在日志与 `/health/ready` 里体现。
- **`REDIS_KEY_PREFIX` 在多套部署共用一个 Redis 时必须各不相同**，否则开发环境的键名冲突会直接干掉生产的会话缓存。
- **`REDIS_SESSION_CACHE_TTL_SECONDS` 刻意远小于 30 天的会话期**：把「撤销未及时传播」的窗口压到一分钟，代价只是每分钟多一次主键查询。
- **`READINESS_REQUIRED_DEPENDENCIES` 只认 `database` / `redis` / `storage` / `provider_keys`**，未知名字忽略并告警。默认只有数据库：它挂了服务真的不能干活；而 Redis 挂了只会降级，对象存储挂了只影响沙箱产物导出。把这些也列为必需，会让一次可恢复的抖动把所有实例一起摘出负载均衡。

## 对象存储

只服务沙箱产物：机器销毁前把 workspace 打包导出到这里，再按预签名 URL 提供下载（唯一用法见 `app/services/sandbox/artifacts.py`）。没开沙箱时整段可以留空。

| 变量 | 默认 | 作用 |
|---|---|---|
| `S3_ENDPOINT_URL` | 空 | S3 API 地址，如 `http://127.0.0.1:9000`（RustFS / MinIO）；留空 = boto3 的默认 AWS 端点 |
| `S3_REGION` | `us-east-1` | 签名 region，与桶实际 region 不一致会被拒为 `AuthorizationHeaderMalformed` |
| `S3_ACCESS_KEY` | 空 | access key，与 secret 成对出现 |
| `S3_SECRET_KEY` | 空 | secret key，生产必须由密钥管理注入 |
| `S3_BUCKET` | `bennett-artifacts` | 产物桶名，必须是小写 DNS 名（3–63 位） |
| `S3_PREFIX` | `sandbox` | 桶内键前缀 |
| `S3_USE_PATH_STYLE` | `true` | 是否用 `endpoint/bucket/key` 寻址 |
| `S3_CREATE_BUCKET` | `true` | 启动时确保桶存在（幂等） |
| `S3_PRESIGN_EXPIRY_SECONDS` | `300` | 下载链接有效期（秒），取值 30–3600 |
| `S3_CONNECT_TIMEOUT` | `3` | 建连超时（秒） |
| `S3_READ_TIMEOUT` | `60` | 读写超时（秒），比对话上游宽松：上传几十 MB 是正常操作 |

- **桶名必须是小写 DNS 名**（正则 `^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`）：带大写或下划线会在建桶时被 S3 直接拒绝，所以配置层先拒绝。
- **`S3_SECRET_KEY` 不写进镜像或 compose 文件**，生产由密钥管理注入。
- **`S3_CREATE_BUCKET=true` 是开发便利**：生产建议关掉，由 IaC 建桶并只授予最小权限，而不是给应用 `s3:CreateBucket`。关掉后启动仍做一次可达性检查，让「端点填错」在日志里暴露。
- **只用固定前缀 + 服务端生成的 ID 拼键**，绝不拼接用户提供的文件名。

## 认证、会话与 Cookie

| 变量 | 默认 | 作用 |
|---|---|---|
| `AUTH_SECRET` | 占位值 | 令牌签名密钥 + API Key 加密密钥 |
| `AUTH_COOKIE_NAME` | `bennett_session` | 会话 cookie 名 |
| `AUTH_COOKIE_SECURE` | `false` | cookie 是否只走 HTTPS |
| `AUTH_COOKIE_SAMESITE` | 空 | `lax` / `strict` / `none`，留空 = 自动 |
| `SESSION_LIFETIME_SECONDS` | `2592000` | 登录态有效期（秒，默认 30 天），下限 60 |
| `REGISTRATION_ENABLED` | `true` | 是否允许公开注册 |

会话走 HttpOnly cookie + 服务端会话表（不用 JWT）。`AUTH_COOKIE_NAME` 改名等于让所有已登录用户掉线，多实例部署必须保持一致。

`SameSite` 留空时自动：凭据模式（`CORS_ALLOW_CREDENTIALS=true`）取 `none`，否则取 `lax`。显式取 `none` 但没开 `AUTH_COOKIE_SECURE` 会**启动即报错**——浏览器会直接丢弃「SameSite=None 但没有 Secure」的 cookie，表现为「登录接口 204、下一个请求 401」。同理，cookie 名带 `__Host-` / `__Secure-` 前缀时也强制要求 `AUTH_COOKIE_SECURE=true`。

### CORS 与来源白名单

| 变量 | 默认 | 作用 |
|---|---|---|
| `CORS_ALLOWED_ORIGINS` | 空 | 逗号分隔的允许来源 |
| `CORS_ALLOW_CREDENTIALS` | `false` | 是否允许浏览器携带 cookie |

留空时只有非 production 环境回退到四个本地前端来源（`http://localhost:5173`、`http://127.0.0.1:5173`、`http://localhost:5174`、`http://127.0.0.1:5174`）；production 留空**不回退**，跨域部署必须显式配置。配置项会去掉尾斜杠、去重、丢掉只剩协议的坏值。

`*` 允许任意来源，但**不能与 `CORS_ALLOW_CREDENTIALS=true` 同开**：浏览器一律拒绝该组合，所以 `app/server.py:create_app` 在启动时直接抛错，而不是静默降级。

同一份来源清单还喂给 `OriginGuardMiddleware`：`/api/*` 的非安全方法（GET/HEAD/OPTIONS/TRACE 之外）若 `Origin` 不在白名单且与 `Host` 不同，返回 403「跨站请求被拒绝」。

### 三条必须记住的规则

**弱 `AUTH_SECRET` 拒绝启动。** 生产环境下 `AUTH_SECRET` 若是默认占位值、含占位词（`change-me` / `replace-with` / `your-` / `example` / `placeholder` / `insecure`），或短于 32 字符，进程直接启动失败，不打警告继续跑。理由很直接：任何人都能用公开的默认密钥伪造重置密码令牌，任意账号可被接管。生成方式：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

**`AUTH_SECRET` 兼作 API Key 加密密钥。** 它经 SHA-256 派生出 Fernet 密钥，用来加密数据库里的提供商 API Key（存储为 `enc:v1:<token>`，读取时透明解密，见 `app/config/secret_box.py`）。代价是：**更换 `AUTH_SECRET` 后，已加密的 API Key 解不开**，需要在管理后台重新填写。升级前写入的明文行不受影响，仍按原样读取，并在下次保存时自动加密。启动日志与 `/health/ready` 会明确提示「有 N 个提供商的 API Key 解密失败」。

**占位 Key 视为未配置。** `TAVILY_API_KEY=tvly-your-api-key` 这类值不算配置成功：搜索工具不会被装配，空对话页也不会推荐搜索类起步示例，能力探针不会谎报「可联网」。识别规则是匹配 `your-api-key` / `your-secret` / `change-me` / `replace-me` / `placeholder` / `example` 这类占位词，不猜格式或长度——各上游 key 形状不同，多加校验只会误杀合法密钥。

## 限流与可信代理

| 变量 | 默认 | 作用 |
|---|---|---|
| `CHAT_RATE_LIMIT_PER_MINUTE` | `30` | 单账号每分钟的对话/标题请求数 |
| `AUTH_RATE_LIMIT_PER_MINUTE` | `10` | 单来源 IP 每分钟的登录/注册数 |
| `CHANGE_PASSWORD_RATE_LIMIT_PER_MINUTE` | `5` | 单账号每分钟的改密请求数 |
| `SYNC_RATE_LIMIT_PER_MINUTE` | `120` | 单账号每分钟的同步请求数 |
| `RATE_LIMIT_BACKEND` | `redis` | `redis`（跨进程共享）/ `memory`（进程内） |
| `RATE_LIMIT_ON_REDIS_ERROR` | `local_fallback` | Redis 不可用时：`local_fallback` / `fail_closed` / `fail_open` |
| `TRUSTED_PROXY_HEADERS` | 空 | 受信任代理的 IP 标头，如 `X-Forwarded-For,X-Real-IP` |
| `TRUSTED_PROXY_IPS` | 空 | 受信任代理的 IP/CIDR 列表 |

四个配额项都是滑动 60 秒窗口，`0` = 不限流。登录与注册各自一个桶（按端点路径分桶），改密单独按账号计数。

改密限流不是可选项：每次验证当前密码是一次约 65ms 的 Argon2 计算，不限流时任意注册用户都能用它打满 CPU，被盗会话也能借此高速爆破当前密码（绕过登录端点的 IP 限流）。

**只配标头名不配 `TRUSTED_PROXY_IPS` 时，标头一律不采信**（fail closed，`app/api/rate_limit.py:resolve_client_ip`）：否则任何能直连后端的人自填一个 `X-Forwarded-For` 就能换限流桶。代价是部署在反向代理之后却不配这两项时，所有请求被算作同一个来源（全站共享一个桶），启动日志会就此告警。两者都配后，只有直连对端落在受信网段内才采信标头，并从右往左跳过受信代理、取第一个非代理地址。

来源地址按 `/64` 归并（IPv6 一个订阅通常是一整个 /64），否则轮换地址就能绕过限流；IPv4-mapped 先解包。

`RATE_LIMIT_ON_REDIS_ERROR` 三种取值都会告警并计入 `redis_unavailable_total`，静默降级是不允许的。`local_fallback` 期间的配额会乘以实例数。

## 沙箱

| 变量 | 默认 | 作用 |
|---|---|---|
| `SANDBOX_ENABLED` | `false` | 总开关 |
| `SANDBOX_PROVIDER` | `opensandbox` | 目前只有这一个取值 |
| `SANDBOX_SERVER_URL` | `http://127.0.0.1:8080` | OpenSandbox server 地址 |
| `SANDBOX_SERVER_API_KEY` | 空 | server 密钥；为空时 server 要求显式确认「无鉴权」 |
| `SANDBOX_TEMPLATE` | `bennett-sandbox` | 模板/镜像名 |
| `SANDBOX_TEMPLATE_VERSION` | `0.1` | 模板版本（落库到租约上，便于追溯） |
| `SANDBOX_CPU` | `2` | 每台机器的 vCPU |
| `SANDBOX_MEMORY_MB` | `4096` | 每台机器的内存（MB），下限 256 |
| `SANDBOX_WORKDIR` | `/home/sandbox/workspace` | 沙箱内的工作目录 |
| `SANDBOX_IDLE_SECONDS` | `600` | 空闲回收：`max(心跳, 最近活动)` 超过它就销毁，下限 60 |
| `SANDBOX_HARD_TTL_SECONDS` | `14400` | 绝对上限（4 小时），到点无论有没有心跳都销毁 |
| `SANDBOX_HEARTBEAT_SECONDS` | `30` | 前端心跳间隔（只写 Redis 的 TTL 键，不写数据库） |
| `SANDBOX_MAX_PER_USER` | `1` | 每用户同时活着的机器数 |
| `SANDBOX_MAX_CONCURRENT` | `12` | 全局并发上限（节点池容量） |
| `SANDBOX_EGRESS` | `proxy` | `proxy` / `open` |
| `SANDBOX_ARTIFACT_MAX_BYTES` | `209715200` | 销毁前导出产物的体积上限（200 MB） |

默认全关：没有 sandbox 服务的部署不该凭空多出四个工具。关闭时能力探针报 `sandbox=false`，模型看不到这些工具，`/api/sandbox/status` 返回 404「未启用执行环境」。

`SANDBOX_MAX_PER_USER` 是容量保护，不是付费分级：超限时工具直接返回「当前执行环境资源已满，请稍后再试」，不排队也不新建（`app/services/sandbox/lease.py:_reject_if_at_capacity`）。并发建机时可能少量超发——加全局锁会把「节点池慢」升级成所有人排队。

`SANDBOX_EGRESS` 目前只是声明：业务代码不读它，出站白名单由 OpenSandbox server 侧的 `[egress]` 配置执行（本地样例见 `docker/opensandbox.toml`）。`open` 只给本地/自用——多租户下自由联网等于把「上传件可能被外带」交给模型决定。

`SANDBOX_SERVER_API_KEY` 为空时，server 需要显式确认无鉴权（`OPENSANDBOX_INSECURE_SERVER=YES`），这个确认只适用于本机开发；生产必须配密钥并只在内网暴露。

## 跨设备同步配额

| 变量 | 默认 | 作用 |
|---|---|---|
| `SYNC_PUSH_MAX_ROWS` | `200` | 单次 push 最多携带的行数（会话 + 消息合计），上限 2000 |
| `SYNC_PULL_MAX_ROWS` | `500` | 单次 pull **每个数据流**最多返回的行数，上限 5000 |
| `SYNC_PULL_MAX_BYTES` | `8388608` | 单次 pull 响应里消息 payload 的累计上限（8 MB） |
| `SYNC_PAYLOAD_MAX_BYTES` | `262144` | 单条消息 payload 序列化后的上限（256 KB） |
| `SYNC_MAX_CONVERSATIONS_PER_USER` | `2000` | 单用户最多保留的未删除会话行数 |
| `SYNC_MAX_MESSAGES_PER_CONVERSATION` | `5000` | 单个会话最多保留的消息行数 |
| `SYNC_LONG_POLL_SECONDS` | `25` | pull 无变更时的挂起上限，0 = 立即返回，上限 120 |

只卡行数挡不住大响应（500 行 × 256 KB 最坏是 128 MB），所以字节上限与行数上限必须同时存在。超限的行按行拒绝，不影响同批其余行；配额类拒绝的语义见[跨设备同步](../features/sync.md)。

## 出站 SSRF 防护

`PROVIDER_BLOCK_PRIVATE_TARGETS` 控制「提供商出站目标是否必须是公网地址」（实现见 `app/services/model_service/outbound_guard.py`）。

- 默认 `false`：本机 LiteLLM 网关（默认地址就是 `http://localhost:4000`）与内网自建 vLLM 都需要它，开了会把它们拦掉。
- 设为 `true` 后，环回/私有/保留段一律拒绝。
- **无论开关如何，链路本地、未指定、组播地址永远被拒绝**——云厂商元数据服务 `169.254.169.254` 就在这一段里。检查还会拆开 IPv6 里内嵌的 IPv4（NAT64、6to4、teredo、IPv4-mapped），否则 `64:ff9b::a9fe:a9fe` 这种写法能绕过。
- 主机名会先解析成地址再判定，所以 `http://2130706433/` 这类十进制写法会先被解析成 `127.0.0.1`，再走同一套判定（严格模式下同样拒绝）。

只在服务对外暴露、需要把「管理员填错地址」的风险压到最低时开启。

### no_proxy

`no_proxy` / `NO_PROXY` 不是 `Settings` 的字段，但进程启动时会先被 `app/config/proxy_env.py:sanitize_no_proxy_env` 规范化，早于任何 HTTP 客户端的构造：方括号 IPv6（`[::1]`、`[::1]:8000`）会被拆成裸地址并去掉端口，仍会让 httpx 拼出非法白名单的条目直接丢弃，两种改写都记 WARNING。`192.168.0.0/16` 这类网段写法不会改值但会告警：httpx 的白名单是 URL 模式，没有网段语义，这些地址仍会走代理。

## 指标、API 文档与日志

| 变量 | 默认 | 作用 |
|---|---|---|
| `METRICS_ENABLED` | `false` | 是否暴露 `/api/metrics`（Prometheus 文本格式） |
| `ENABLE_API_DOCS` | 空 | 是否暴露 `/docs`、`/redoc`、`/openapi.json` |
| `LOG_LEVEL` | `INFO` | `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`，配错退回 INFO |
| `LOG_ACCESS` | 关闭 | 是否输出 uvicorn 访问日志 |
| `ASSISTANT_NAME` | `Bennett` | 展示给用户的助手名称，同时进 API 标题 |
| `MAX_REQUEST_BODY_BYTES` | `33554432` | 单个 HTTP 请求体硬上限（32 MB），下限 1024 |

`ENABLE_API_DOCS` 留空 = 自动：`APP_ENV=production` 时关闭，其余环境开启。文档页把端点结构白送给未认证访客，生产默认关掉；确实需要时再显式打开，并挡在内网或反代白名单后面。

`METRICS_ENABLED` 默认关闭同理：它不需要登录就能访问，多开一个端点就多一分侦察面。关闭时端点返回 **404** 而不是 403——404 比「存在但你不能看」泄露得更少。

`LOG_LEVEL` 与 `LOG_ACCESS` **不经过 `Settings`**：`app/config/logging_setup.py` 直接读进程环境变量，而 pydantic-settings 只把 dotenv 的值灌进 `Settings`、不改 `os.environ`（仓库里也没有 `load_dotenv()`）。所以这两项写在 `.env*` 文件里不生效，要 `export` 或写进 compose 的 `environment`。

`LOG_LEVEL=DEBUG` 下第三方库（httpx / openai / httpcore / urllib3 / asyncio）仍被压在 WARNING：它们会把请求头打进日志，里面就是 Authorization 与 API Key。访问日志默认关闭，因为 SSE 长连接与前端轮询会让它变成噪音，真正要看的 WARNING 会被淹没。

`MAX_REQUEST_BODY_BYTES` 是一道必要的闸：Starlette/uvicorn 默认不限制请求体，同步接口的单条 payload 校验发生在 JSON 完整解析之后，没有这道闸时任意已登录用户（注册开放）都能用超大 body 把进程内存打满。超限返回 413，且不把 body 读进内存。反向代理上建议再配一层更小的值。

## 启动期守卫

以下情况进程直接起不来。这些检查全部在启动期完成，不会等到第一条请求才暴露。

| 触发条件 | 结果 | 依据 |
|---|---|---|
| `APP_ENV` 不在四个白名单值内 | 启动失败 | `settings.py:_normalise_app_env` |
| `DATABASE_URL` 不以 `postgresql+asyncpg://` 开头 | 启动失败 | `settings.py:_require_postgres_driver` |
| `S3_BUCKET` 不是小写 DNS 名（3–63 位） | 启动失败 | `settings.py:_validate_bucket_name` |
| 任一数值项越界（如 `SANDBOX_MEMORY_MB < 256`、`CONTEXT_SAFETY_RATIO > 1`） | 启动失败 | 各字段的 `Field(ge=…, le=…)` |
| `APP_ENV=production` 且 `AUTH_SECRET` 弱（默认值/占位词/短于 32 字符） | 启动失败 | `settings.py:_reject_placeholder_secret_in_production` |
| `APP_ENV=production` 且 `AUTH_COOKIE_SECURE=false` | 启动失败 | `settings.py:_reject_insecure_cookie_in_production` |
| `AUTH_COOKIE_NAME` 含非法字符，或用 `__Host-` / `__Secure-` 前缀却没开 Secure | 启动失败 | `settings.py:_validate_cookie_name_prefix` |
| 生效的 SameSite 是 `none`（显式配置或由 `CORS_ALLOW_CREDENTIALS=true` 推导）而没开 Secure | 启动失败 | `settings.py:_reject_samesite_none_without_secure` |
| `CORS_ALLOW_CREDENTIALS=true` 且 `CORS_ALLOWED_ORIGINS` 含 `*` | 启动失败 | `app/server.py:create_app` |
| `MCP_SERVERS` 任一条声明格式错、名称非法或重名 | 启动失败 | `app/tools/mcp_client.py:load_server_configs` |
| 提示词模板编译或渲染失败（缺变量、渲染成空串） | 启动失败 | `app/server.py:validate_prompts` |
| 数据库不可达，或模型有、库里没有的列（`DATABASE_APPLY_SCHEMA=true`） | 启动失败 | `app/db/schema.py:verify_schema_at_head` |

反过来，这些失败**只记日志、不拦启动**：提供商引导、过期会话清理、API Key 解密自检、对象存储可达性检查、MCP 工具发现。它们都不该把一次可恢复的抖动升级成起不来。

## 相关源码

- `app/config/settings.py`
- `app/config/secret_box.py`
- `app/config/logging_setup.py`
- `app/config/proxy_env.py`
- `app/server.py`
- `app/middleware.py`
- `app/api/rate_limit.py`
- `app/api/ops.py`
- `app/db/schema.py`
- `app/services/context_compaction.py`
- `app/services/token_budget.py`
- `app/services/upstream_errors.py`
- `app/services/model_service/outbound_guard.py`
- `.env.example`

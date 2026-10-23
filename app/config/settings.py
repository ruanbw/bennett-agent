import os
import re
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

DEFAULT_APP_ENV = "development"

#: 合法的环境名白名单。拼错（prod/prd/Productionn）会让 .env.{env} 静默加载不到，
#: 所以不认识的取值必须报错，而不是悄悄退化成一个跑得起来但配置不对的进程。
ALLOWED_APP_ENVS = frozenset({"development", "production", "staging", "test"})

#: 认证密钥的占位默认值。生产环境若仍是这个值，启动时直接报错而不是静默放行。
DEFAULT_AUTH_SECRET = "dev-insecure-secret-change-me"

#: 桶名正则（S3 兼容规则）。不匹配的桶名在 AWS 上建不出来，在自建网关上是未定义行为。
S3_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

#: 合法 cookie 名（RFC 6265 token）。前缀 `__Host-` / `__Secure-` 也落在这个集合里。
COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

#: 生产密钥的最小长度与占位词。只挡默认值不够：.env.production 里抄一句
#: "replace-with-a-strong-random-value" 一样没有任何安全强度。
MIN_PRODUCTION_SECRET_LENGTH = 32
_PLACEHOLDER_SECRET_PATTERN = re.compile(
    r"change-me|replace-with|your-|example|placeholder|insecure",
    re.IGNORECASE,
)


def _is_weak_auth_secret(secret: str) -> bool:
    """密钥是否弱：默认占位值、含占位词（大小写不敏感）、或长度不足。"""
    if secret == DEFAULT_AUTH_SECRET or len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
        return True
    return bool(_PLACEHOLDER_SECRET_PATTERN.search(secret))


#: API Key 的占位词。照抄 `.env.example` 的 `tvly-your-api-key` 时，值非空，工具
#: 会「看起来已配置」并被装配进 Agent；真正调用时才 401，白烧一轮工具调用与 token。
#: 识别成「未配置」比让它进装配更诚实，也让能力探针（`/api/capabilities`）不会谎报。
_PLACEHOLDER_API_KEY_PATTERN = re.compile(
    r"your[-_ ]?(?:api[-_]?)?key|your[-_ ]?secret|change[-_]?me|replace[-_]?me|placeholder|example",
    re.IGNORECASE,
)


def is_usable_api_key(value: str | None) -> bool:
    """配置的 API Key 是否可用：非空白，且不是示例里的占位值。

    只判「有没有配置」，不猜格式或长度——各上游的 key 形状不同，多加校验只会误杀
    合法密钥。占位值是唯一例外：它非空却在功能上等于没配（见上面的正则）。
    """
    if value is None:
        return False
    cleaned = value.strip()
    if not cleaned:
        return False
    return not _PLACEHOLDER_API_KEY_PATTERN.search(cleaned)


# 只有「文件名不依赖 APP_ENV」的文件能参与判定环境名本身：
# .env.{env} 的文件名由 APP_ENV 决定，读它来选它会形成循环依赖。
_ENV_NAME_SOURCES = (".env.local", ".env")


def _read_app_env_from_file(path: Path) -> str | None:
    """从单个 dotenv 文件读取 APP_ENV；文件不存在或未设置时返回 None。"""
    if not path.is_file():
        return None
    value = dotenv_values(path).get("APP_ENV")
    if value is None or not value.strip():
        return None
    return value.strip().lower()


def resolve_app_env(base_dir: Path = BASE_DIR) -> str:
    """确定当前运行环境名。

    APP_ENV 既可以由 shell 导出，也可以写在 .env / .env.local 里。后者是必需的：
    get_env_files() 必须在 pydantic-settings 加载任何 dotenv 文件**之前**就知道该
    加载哪些文件，否则写进 .env 的 APP_ENV 会被静默忽略，而 .env.development 里自带的
    APP_ENV=development 还会反过来覆盖运维的意图。

    优先级：系统环境变量 > .env.local > .env > "development"
    """
    from_process = os.getenv("APP_ENV")
    if from_process and from_process.strip():
        return from_process.strip().lower()

    for name in _ENV_NAME_SOURCES:
        from_file = _read_app_env_from_file(base_dir / name)
        if from_file:
            return from_file

    return DEFAULT_APP_ENV


def get_env_files(base_dir: Path = BASE_DIR) -> tuple[Path, ...]:
    """根据 APP_ENV 动态解析环境变量文件加载顺序。

    pydantic-settings 对 env_file 元组采用「靠后者覆盖靠前者」，因此本元组按优先级
    从低到高排列，系统环境变量始终高于所有文件：

        APP_ENV 环境变量 > .env.{APP_ENV}.local > .env.{APP_ENV} > .env.local > .env
    """
    env = resolve_app_env(base_dir)
    env_files: list[Path] = [
        base_dir / ".env",
        base_dir / ".env.local",
        base_dir / f".env.{env}",
        base_dir / f".env.{env}.local",
    ]
    return tuple(f for f in env_files if f.is_file())


class Settings(BaseSettings):
    """全局应用配置"""

    app_env: str = Field(
        default=DEFAULT_APP_ENV,
        description=(
            "运行环境，只接受 development/production/staging/test（大小写与首尾空格会被归一化）。"
            "它决定额外加载哪个 .env.{env}，因此拼错时直接报错而不是静默回落。"
        ),
    )
    enable_api_docs: bool | None = Field(
        default=None,
        description=(
            "是否暴露 /docs、/redoc、/openapi.json。None=自动：production 关闭，其余环境开启；"
            "显式 true/false 可覆盖自动判断。"
        ),
    )
    litellm_api_base: str = Field(default="http://localhost:4000", description="LiteLLM Proxy API 基础地址")
    litellm_api_key: str | None = Field(default=None, description="LiteLLM API 密钥")
    model_name: str = Field(default="openai/gpt-4o", description="模型标识符（LiteLLM 路由格式）")
    fallback_model_name: str | None = Field(
        default=None,
        description=(
            "备用模型标识符（LiteLLM 路由格式）。主模型返回 5xx/429、超时或连接失败时"
            "自动切换一次；留空表示不启用。备用模型必须接受与主模型相同的调用参数"
            "（如 reasoning_effort 与上游超时），否则切换会以 400 失败。"
        ),
    )
    model_description_field: str = Field(
        default="magpie_label",
        description=(
            "上游模型条目里用作展示描述的字段名；缺失时回退为 'provider name'。"
            "做成配置项而不是常量，否则一个自称网关无关的模块会耦合到特定网关的字段名。"
        ),
    )
    assistant_name: str = Field(default="Bennett", description="助手名称")
    tavily_api_key: str | None = Field(default=None, description="Tavily 网页搜索 API Key")
    mcp_servers: str = Field(
        default="",
        description=(
            "MCP server 声明，分号分隔。每条形如 '名称=url:https://host/mcp' 或 "
            "'名称=stdio:/path/to/server arg1 arg2'。名称只能用字母/数字/下划线/短横线"
            "（它会变成工具名前缀，如 `filesystem__read_file`）。留空 = 不接任何 MCP server。"
            "写错的声明会让**启动失败**而不是静默跳过：声明出来的能力不能悄悄消失。"
        ),
    )
    embedding_model: str | None = Field(
        default=None,
        description=(
            "知识库（RAG）用的 embedding 模型 id，例如 `text-embedding-3-small`。"
            "地址与密钥复用**默认提供商**（与对话同一个网关），因此不另配 base_url。"
            "留空或仍是占位值时知识库整体不可用：入库与检索都会被跳过，对话照常。"
            "换模型后必须重建索引——旧向量的维度与语义空间都不同，混用会让检索"
            "静默返回不相关的结果。"
        ),
    )
    rag_top_k: int = Field(
        default=4,
        ge=1,
        le=8,
        description=(
            "每轮对话自动检索的知识库片段数上限。每多一块就多占一份上下文预算；"
            "四块（约 3k token）已能覆盖绝大多数问题。"
        ),
    )
    rag_min_score: float = Field(
        default=0.15,
        ge=0,
        le=1,
        description=(
            "检索结果的最低混合分（向量 0.7 + 词法 0.3 加权后）。低于它的候选不返回："
            "一条勉强相关的结果比没有结果糟——模型会拿它当依据编答案。"
        ),
    )
    metrics_enabled: bool = Field(
        default=False,
        description=(
            "是否暴露 /api/metrics（进程内指标，Prometheus 文本格式）。"
            "**默认关闭**：它不需要登录就能访问，多开一个端点就多一分侦察面，而多数部署"
            "并没有采集器。启用后建议同时把 /api/metrics 挡在内网或反代白名单后面。"
        ),
    )
    mcp_tool_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=600.0,
        description=(
            "单个 MCP 工具调用的默认超时（秒）。MCP server 可能做很重的事（跑查询、读大文件），"
            "所以比搜索宽；但必须有上限——没有上限时一个卡死的 server 会一直占着一个 SSE。"
        ),
    )
    cors_allowed_origins: str = Field(
        default="",
        description=(
            "逗号分隔的 CORS 允许来源；留空时开发环境回退本地前端端口，production 不回退"
            "（跨域部署必须显式配置）。设为 * 时允许任意源，但不能与凭据同开。"
        ),
    )
    cors_allow_credentials: bool = Field(
        default=False,
        description=(
            "CORS 是否允许浏览器携带凭据（cookie）。设为 true 时 cors_allowed_origins 不能含 *"
            "（浏览器会直接拒绝该组合），且跨站 cookie 需要 SameSite=None + Secure。"
        ),
    )
    chat_rate_limit_per_minute: int = Field(
        default=30,
        ge=0,
        description="单个**账号**每分钟可发起的对话/标题请求数；0 表示不限流。",
    )
    auth_rate_limit_per_minute: int = Field(
        default=10,
        ge=0,
        description=("单个**来源 IP** 每分钟可发起的登录/注册请求数；0 表示不限流。挡的是密码暴力破解与批量注册。"),
    )
    registration_enabled: bool = Field(
        default=True,
        description=(
            "是否允许公开注册。默认开启（开箱即用）；私有/内部部署建议设为 false，"
            "并用 scripts/seed_admin.py 建号，避免任何人批量注册 + 绕过按账号限流。"
        ),
    )
    change_password_rate_limit_per_minute: int = Field(
        default=5,
        ge=0,
        description=(
            "单个**账号**每分钟可发起的改密请求数；0 表示不限流。改密要验证当前密码，"
            "每次验证是约 65ms 的 Argon2 计算：不限流时任意注册用户都能用它打满 CPU，"
            "被盗会话也能借此高速爆破当前密码（绕过登录端点的 IP 限流）。"
        ),
    )
    trusted_proxy_headers: str = Field(
        default="",
        description=(
            "逗号分隔的受信任反向代理 IP 标头列表（如 'X-Forwarded-For,X-Real-IP'）。"
            "仅当部署在受信反向代理之后时配置；**必须同时配置 TRUSTED_PROXY_IPS**，"
            "否则标头不会被采信（避免直连客户端伪造 XFF 绕过限流）。"
        ),
    )
    trusted_proxy_ips: str = Field(
        default="",
        description=(
            "受信反向代理的 IP/CIDR 列表（逗号分隔，如 '10.0.0.0/8,127.0.0.1/32'）。"
            "只有直连对端落在这些网段内时才读 TRUSTED_PROXY_HEADERS，并从右往左跳过"
            "受信代理地址、取第一个非代理地址作为客户端 IP。为空时一律使用 direct host。"
        ),
    )
    rate_limit_backend: Literal["redis", "memory"] = Field(
        default="redis",
        description=(
            "限流计数存储：redis = 跨进程/多实例的共享配额（生产默认）；"
            "memory = 进程内（单 worker 够用，测试更快）。"
            "redis 不可用时自动降级为进程内计数并在日志里报警——可用性优先，"
            "但降级期间的配额是乘以实例数的，这一点必须在告警里说清。"
        ),
    )
    rate_limit_on_redis_error: Literal["local_fallback", "fail_closed", "fail_open"] = Field(
        default="local_fallback",
        description=(
            "Redis 限流不可用时的处置："
            "local_fallback = 退到进程内计数（配额乘以实例数，但仍然有约束，默认）；"
            "fail_closed = 一律 429（可用性换确定性，需 Redis 自身高可用）；"
            "fail_open = 一律放行（仅当限流是纯优化时合理）。"
            "三种都会告警并计入 redis_unavailable_total，静默降级是不允许的。"
        ),
    )
    model_upstream_timeout: float = Field(
        default=120.0,
        ge=0,
        description=(
            "上游模型调用的**空闲**超时（秒）：多久没收到数据就放弃。不是总时长，"
            "因此不会提前掉长长的流式输出。0 表示不限制。"
        ),
    )
    max_request_body_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024,
        description=(
            "单个 HTTP 请求体的硬上限（字节）。没有这一道时，任意已登录用户（注册开放）"
            "都能用超大 body 把进程内存打满：Starlette/uvicorn 默认不限制请求体，"
            "而同步接口的单条 payload 校验发生在 JSON **完整解析之后**。超限返回 413，"
            "且不会把 body 读进内存。反向代理上建议再配一层更小的值。"
        ),
    )

    # ------------------------------------------------------------------
    # 上下文窗口预算
    # ------------------------------------------------------------------
    context_guard_mode: Literal["off", "reject", "compact"] = Field(
        default="compact",
        description=(
            "历史超出模型上下文窗口时的处置："
            "off = 不判定（一律放行）；"
            "compact = 先压缩（截断超长单条 → 折叠早期轮次成摘要），压完仍超才拒绝；"
            "reject = 直接拒绝本轮并告知用户。"
            "窗口取自网关的 context_window，网关没报时一律放行。"
            "默认 compact：reject 会把用户的路走死（他的消息已落库，下一轮只会更长），"
            "而绝大多数超限里占窗口的是可以压掉的历史工具结果与旧轮次。"
        ),
    )
    context_reserve_tokens: int = Field(
        default=8192,
        ge=0,
        description=(
            "预留 token 数（留给模型写回复的空间），在网关没报 max_output_tokens 时生效；"
            "报了就用上游声明的值。两种取值都会被压在 context_window 的一半以内，"
            "避免窗口小的模型被预留吃光预算。"
        ),
    )
    context_safety_ratio: float = Field(
        default=0.9,
        gt=0,
        le=1,
        description=(
            "预算的安全系数：可用上限 = (窗口 - 预留 - 固定开销) × 本值。token 数是估算值"
            "（无分词器依赖，见 app/services/token_budget.py），留一成余量避免贴着上限判断。"
        ),
    )
    context_compact_keep_recent_turns: int = Field(
        default=4,
        ge=1,
        description=(
            "压缩时保留最近多少**轮**问答原文（一轮 = user + assistant 两条）。"
            "更早的部分被折叠成摘要。调大 = 细节保留更多、能压出的空间更少；"
            "调小 = 省更多窗口、但模型更容易忘记刚才说过的事。"
            "压缩只在超预算时才发生，正常长度的会话不受影响。"
        ),
    )
    context_compact_message_max_chars: int = Field(
        default=4_000,
        ge=500,
        description=(
            "压缩第一级（截断超长单条）的单条字符上限：保留首尾、中间打省略标记。"
            "只作用于本轮问题之外的旧消息——截断最后一条等于替用户改问题。"
            "取 4000 是因为超过它的单条几乎都是粘贴的日志/代码，而它们的两端才是结论。"
        ),
    )

    # ------------------------------------------------------------------
    # 数据库与认证
    # ------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://bennett:bennett@127.0.0.1:5432/bennett",
        description=(
            "SQLAlchemy 异步连接串，必须是 `postgresql+asyncpg://`。"
            "SQLite 已从支持矩阵移除：并发写入、外键语义、迁移工具链都不足以支撑"
            "多实例部署（见 app/db.py 的模块说明）。"
        ),
    )
    database_pool_size: int = Field(
        default=10,
        ge=1,
        description=(
            "连接池常驻连接数。每个 uvicorn worker 各有一份，**总连接数 = pool_size + "
            "max_overflow 再乘以 worker 数**，必须小于 Postgres 的 max_connections"
            "（默认 100）：4 个 worker × (10+10) 正好打满，是「一扩容就全站 500」的经典成因。"
        ),
    )
    database_max_overflow: int = Field(
        default=10,
        ge=0,
        description="池满之后允许临时新建的连接数；超出后请求等待 pool_timeout 再报错。",
    )
    database_connect_timeout: float = Field(
        default=5.0,
        gt=0,
        description=(
            "建连超时（秒）。asyncpg 默认 60 秒：数据库不可达时，一个请求会挂一分钟看不出原因，"
            "然后才报一个跟“数据库没起来”毫无关系的 `TimeoutError`。"
        ),
    )
    database_pool_timeout: float = Field(
        default=10.0,
        gt=0,
        description=(
            "等空闲连接的最长秒数。**必须明显小于客户端/反代的超时**："
            "等 30s 再报错时，用户侧的连接早就断了，日志里只剩一堆无意义的 500。"
        ),
    )
    database_pool_recycle: int = Field(
        default=1800,
        ge=0,
        description=(
            "连接的最长存活秒数（0=不回收）。Postgres 侧或中间的连接代理（pgbouncer/云"
            "负载均衡）会单方面掐掉空闲连接，回收周期必须短于那个静默超时。"
        ),
    )
    database_statement_timeout_ms: int = Field(
        default=30_000,
        ge=0,
        description=(
            "服务端语句超时（毫秒，0=不限制），随连接下发。应用层超时只能放弃等待、"
            "**请求仍在数据库里跑着**；服务端超时才会真正取消它，避免一条烂查询把连接池耗干。"
        ),
    )
    database_pool_mode: Literal["pool", "null"] = Field(
        default="pool",
        description=(
            "连接池策略。`pool` = 常驻连接池（服务进程）；`null` = 每次取用都新建连接。"
            "`null` 用于两种场景：①短命进程（迁移/脚本/CLI），省掉池的初始化与回收；"
            "②测试里的同步 `TestClient`——它在**另一个线程的事件循环**里执行请求，"
            "而池里的 asyncpg 连接绑定在创建它的事件循环上，跨循环复用会抛"
            "`Future attached to a different loop`，报错点与真正原因毫无关系。"
        ),
    )
    database_require_head_revision: bool = Field(
        default=True,
        description=(
            "启动时校验库结构是否已到 Alembic 的 head，未到则拒绝启动。"
            "迁移交给部署步骤（compose 的 migrate 服务 / CI），**不在应用启动时自动升级**："
            "多实例滚动发布时，自动升级会让「旧代码 + 新结构」的窗口无法避免。"
            "本地开发想省一步可以设 false，但要清楚它意味着可能带着旧结构跑新代码。"
        ),
    )
    # ------------------------------------------------------------------
    # Redis（限流计数 / 业务缓存 / 会话热缓存）
    # ------------------------------------------------------------------
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        description=(
            "Redis 连接串。**Redis 不是可用性单点**：每一处使用都有明确的降级路径（限流退化为"
            "进程内计数、缓存 miss、会话回源数据库），但这些降级必须能在日志与就绪探针里看到，"
            "否则“限流悄悄失效”会变成一个没人知道的漏洞。"
        ),
    )
    redis_key_prefix: str = Field(
        default="bennett",
        description=(
            "所有键的公共前缀。多套部署共用一个 Redis 实例时必须各不相同——"
            "否则开发环境的 flush 或键名冲突会直接干掉生产的会话缓存。"
        ),
    )
    redis_connect_timeout: float = Field(
        default=2.0,
        gt=0,
        description=(
            "建连超时（秒）。必须短：Redis 不可达时每个请求都要白等这么久，而它只是限流/缓存，不该拖垮对话主链路。"
        ),
    )
    redis_socket_timeout: float = Field(
        default=1.0,
        gt=0,
        description="单次命令的读写超时（秒）。与上面同理：宁可降级，不要挂住。",
    )
    redis_max_connections: int = Field(
        default=50,
        ge=1,
        description=(
            "连接池上限。每个 uvicorn worker（以及 worker 进程）各自一份："
            "总连接数 = 本值 × 进程数，必须小于 Redis 的 `maxclients`（默认 10000，通常不是瓶颈）。"
        ),
    )
    redis_session_cache_enabled: bool = Field(
        default=True,
        description=(
            "是否用 Redis 加速会话 token → 用户的解析。**数据库仍是权威来源**："
            "Redis 只是 cache-aside，删会话时两边都删。这样 Redis 重启不会把所有人踢下线，"
            "而即时登出/改密失效依然成立（见 app/db.py 的 CachedAccessTokenDatabase）。"
        ),
    )
    redis_session_cache_ttl_seconds: int = Field(
        default=60,
        ge=1,
        description=(
            "会话缓存的最长存活秒数（同时不得超过会话剩余寿命）。刻意远小于 30 天的会话期："
            "把「撤销未及时传播」（进程崩溃、网络分区、缓存删除失败）的窗口压到一分钟，"
            "代价只是每分钟多一次主键查询。"
        ),
    )
    search_cache_backend: Literal["redis", "memory"] = Field(
        default="redis",
        description=(
            "Tavily 搜索缓存的存放位置。redis = 同实例多 worker 共享（命中率更高，也更省配额）；"
            "memory = 进程内 LRU（Redis 不可用时的降级形态，不会报错）。"
        ),
    )
    readiness_required_dependencies: str = Field(
        default="database",
        description=(
            "就绪探针里**必须可用**的依赖（逗号分隔，可选 database/redis/storage）。"
            "默认只有数据库：它挂了服务真的不能干活；而 Redis 挂了只会降级（限流变进程内、"
            "会话回源库），对象存储挂了只影响上传。把这些也列为必需会让一次可恢复的抖动"
            "把所有实例一起摘出负载均衡，把降级变成全站故障。"
        ),
    )

    # ------------------------------------------------------------------
    # 对象存储（S3 / RustFS）：上传文件的原件
    # ------------------------------------------------------------------
    s3_endpoint_url: str = Field(
        default="",
        description=(
            "S3 API 地址，如 `http://127.0.0.1:9000`（RustFS / MinIO）。"
            "留空则用 boto3 的默认 AWS 端点——仅在真的用 AWS S3 时成立。"
        ),
    )
    s3_region: str = Field(
        default="us-east-1",
        description=(
            "签名 region。RustFS 默认 `us-east-1`；与真实桶所在 region 不一致时"
            "会被拒绝为 `AuthorizationHeaderMalformed`，而错误信息不会告诉你改哪个配置。"
        ),
    )
    s3_access_key: str = Field(default="", description="S3 / RustFS 的 access key（与 secret 成对出现）。")
    s3_secret_key: str = Field(
        default="",
        description="S3 / RustFS 的 secret key。**生产必须由密钥管理注入**，不写进镜像或 compose 文件。",
    )
    s3_bucket: str = Field(
        default="bennett-uploads",
        description=(
            "上传文件的原件桶。桶名全局唯一（跨账号），且必须是小写 DNS 兼容名——"
            "带下划线或大写会在建桶时被 S3 直接拒绝。"
        ),
    )
    s3_prefix: str = Field(
        default="uploads",
        description=(
            "桶内键前缀。**只用固定前缀 + 服务端生成的 UUID 拼键，绝不拼接用户文件名**："
            "用户文件名里的 `../`、控制字符或超长中文会在对象键里变成另一层注入面。"
        ),
    )
    s3_use_path_style: bool = Field(
        default=True,
        description=(
            "是否用 path-style 寻址（`endpoint/bucket/key`）。RustFS / MinIO / 大多数自建"
            "S3 网关**只支持 path-style**；AWS 官方 S3 建议关掉（virtual-hosted 走 TLS 证书通配）。"
        ),
    )
    s3_create_bucket: bool = Field(
        default=True,
        description=(
            "启动时确保桶存在（幂等）。开发环境省一步手工操作；生产建议关掉，"
            "由 IaC 建桶并单独授予 `s3:PutObject` 等最小权限，而不是给应用 `s3:CreateBucket`。"
        ),
    )
    s3_presign_expiry_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description=(
            "下载原件的预签名 URL 有效期（秒）。默认 5 分钟：链接会被浏览器历史/日志/"
            "团队群聊留存，有效期过长等于把私有对象变成半公开。"
        ),
    )
    s3_connect_timeout: float = Field(default=3.0, gt=0, description="S3 建连超时（秒）。")
    s3_read_timeout: float = Field(
        default=60.0,
        gt=0,
        description="S3 读写超时（秒）。比对话上游宽松：上传几十 MB 文件是正常操作，不是故障。",
    )

    # ------------------------------------------------------------------
    # 文件上传（原件 → 对象存储 → 解析 → 知识库）
    # ------------------------------------------------------------------
    uploads_enabled: bool = Field(
        default=True,
        description=(
            "是否开放上传接口。关闭时端点返回 404（而不是 403）："
            "对未启用的部署，声明“不存在”比“存在但你不能用”泄露更少。"
        ),
    )
    upload_max_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        description=(
            "单个文件的大小上限（字节）。**在流式读取过程中累计计数并在超限时立即中止**，"
            "而不是先收完再判断——后者等于让任何人用 10GB 的 body 把磁盘/内存吃满。"
        ),
    )
    upload_allowed_extensions: str = Field(
        default="pdf,docx,md,txt,html,htm",
        description=(
            "允许的扩展名白名单（逗号分隔）。同时校验扩展名与嗅探到的 MIME："
            "只看扩展名挡不住改名上传，只看 MIME 又不可靠（很多环境把一切非标准类型报成"
            "application/octet-stream）。"
        ),
    )
    upload_max_files_per_user: int = Field(
        default=200,
        ge=1,
        description="单用户最多保留的上传记录数（含已失败）。防把对象存储当无限盘用。",
    )
    upload_total_bytes_per_user: int = Field(
        default=1024 * 1024 * 1024,
        ge=1024,
        description="单用户上传原件累计字节上限（按未删除的记录求和）。",
    )
    upload_max_extracted_chars: int = Field(
        default=200_000,
        ge=10_000,
        description=(
            "单个文件允许入库的抽取文本上限（字符）。**必须不超过知识库的单篇上限**"
            "（app/services/rag/store.py 的 MAX_DOCUMENT_CHARS，200k）：否则一份超长文档"
            "会先烧完 embedding 配额再被入库层拒绝，用户等了几分钟只拿到一个错。"
            "超限时解析会明确失败并提示拆分文件，而不是静默截断——被截掉的后半篇"
            "用户永远不知道它不在索引里。"
        ),
    )
    upload_worker_poll_seconds: float = Field(
        default=2.0,
        gt=0,
        description=(
            "解析 worker 在没有待处理任务时的轮询间隔（秒）。与 PostgreSQL 的"
            "`LISTEN/NOTIFY` 相比这是朴素的轮询，但它在多实例、断连重连、重启恢复上都只有"
            "一条路径要验证，且 2 秒延迟对“上传后后台解析”完全可接受。"
        ),
    )
    upload_max_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "单个文件解析失败后最多重试几次。超过后置为 failed 并把原因写进记录："
            "无限重试会让一份坏文件永远占着 worker，且每次重试都在烧 embedding 配额。"
        ),
    )

    auth_secret: str = Field(
        default=DEFAULT_AUTH_SECRET,
        description=(
            "重置密码/邮箱验证令牌的签名密钥，同时用于**加密数据库里的提供商 API Key**。"
            "生产必须替换为 ≥32 字符的强随机串；**更换它会让已加密的 API Key 无法解密**，"
            "需要在管理后台重新填写（旧明文行不受影响，仍按原样读取）。"
        ),
    )
    auth_cookie_name: str = Field(default="bennett_session", description="会话 cookie 名。")
    auth_cookie_secure: bool = Field(
        default=False,
        description=(
            "cookie 是否只走 HTTPS。**本地 http 调试必须保持 false**，否则浏览器"
            "不会回传 cookie，表现为「登录成功了但下一个请求又是未登录」；"
            "生产（HTTPS）必须设为 true。"
        ),
    )
    auth_cookie_samesite: Literal["lax", "strict", "none"] | None = Field(
        default=None,
        description=(
            "会话 cookie 的 SameSite。None=自动：cors_allow_credentials=true 时取 none"
            "（跨站部署下 lax 的 cookie 根本不会被浏览器回传），否则取 lax。"
            "取值 none 必须同时满足 auth_cookie_secure=true——浏览器会直接丢弃"
            "「SameSite=None 但没有 Secure」的 cookie，表现为「登录接口 204 但下一个请求 401」。"
        ),
    )
    session_lifetime_seconds: int = Field(
        default=30 * 24 * 60 * 60,
        ge=60,
        description=(
            "登录态有效期（秒），默认 30 天，下限 60：0 会让 fastapi-users 认为「不设过期」"
            "（服务端永不过期），而 cookie Max-Age=0 又会立即删除 cookie，语义互相矛盾。"
        ),
    )
    provider_block_private_targets: bool = Field(
        default=False,
        description=(
            "提供商出站目标是否必须为公网地址（环回/私有/保留段一律拒绝）。默认关闭："
            "本机 LiteLLM 网关与自建 vLLM 常在同一台机器或内网，开启会把文档里最主要的"
            "用法拦掉。无论开关如何，链路本地/未指定/组播地址（含云厂商元数据服务）"
            "永远被拒绝。仅在服务对外暴露、需要把「管理员填错地址」的风险压到最低时开启。"
        ),
    )
    bootstrap_provider_from_env: bool = Field(
        default=True,
        description=(
            "首次启动（全新库）时是否用 LITELLM_API_BASE / MODEL_NAME 创建一条「默认网关」"
            "提供商。关闭后管理后台从空白开始配置。首次引导或首次在后台创建提供商时会写入"
            "初始化标记，该标记写入后永不清除，因此**删光提供商后重启不会再次引导**——"
            "需要撤回配置后重新开始时，只能在管理后台手动重新添加；"
            "要临时停用某个提供商请设为「禁用」，而不是删除。"
        ),
    )

    # ------------------------------------------------------------------
    # 跨设备同步
    # ------------------------------------------------------------------
    sync_rate_limit_per_minute: int = Field(
        default=120,
        ge=0,
        description=(
            "单个**账号**每分钟可发起的同步请求数（push + pull 共用一个桶）；0 表示不限流。"
            "单独一个桶，不扰动对话配额：对话消耗上游额度，同步不消耗。"
        ),
    )
    sync_push_max_rows: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="单次 push 最多携带的行数（会话 + 消息合计）。客户端按此分批。",
    )
    sync_pull_max_rows: int = Field(
        default=500,
        ge=1,
        le=5000,
        description=(
            "单次 pull 每个数据流（会话 / 消息）最多返回的行数。注意这不是总量："
            "游标只有在两路都拉完时才前进，所以分页会在下一次 pull 继续。"
        ),
    )
    sync_pull_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        description=(
            "单次 pull 响应里消息 payload 的累计上限（字节）。只卡行数挡不住大响应："
            "500 行 × 256 KB 最坏是 128 MB，一次就能把服务端序列化与浏览器主线程一起拖死。"
        ),
    )
    sync_payload_max_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        description=(
            "单条消息 payload 序列化后的上限（字节）。前端把正文、思考链、工具调用与结果"
            "整包放在 payload 里，所以不能只卡正文字段；超限的行按行拒绝，不影响同批其余行。"
        ),
    )
    sync_max_conversations_per_user: int = Field(
        default=2000,
        ge=1,
        description=("单用户最多保留的**未删除**会话行数，防止把服务端当无限存储桶。"),
    )
    sync_max_messages_per_conversation: int = Field(
        default=5000,
        ge=1,
        description=("单个会话最多保留的消息行数，超出的新行按 quota_exceeded 拒绝。"),
    )
    sync_long_poll_seconds: float = Field(
        default=25.0,
        ge=0,
        le=120,
        description=(
            "pull 无变更时长轮询的挂起上限（秒），0 表示不挂起（立即返回）。"
            "挂起连接占用一个协程与一条 HTTP 连接，单进程部署下这个量级可以接受；"
            "多 worker 时需要用 SSE 或共享 pub/sub 才能正确唤醒。"
        ),
    )

    @field_validator("database_url")
    @classmethod
    def _require_postgres_driver(cls, value: str) -> str:
        """只接受 `postgresql+asyncpg://`。

        为什么在配置层硬性拒绝（而不是“跑起来再看”）：SQLite 曾经是支持项，升级后很
        容易留下一个 `sqlite+aiosqlite://` 的旧 `.env`。它不会报错，会**静默建出一个
        空库**——表现为“所有账号都没了”，而日志里一切正常。把错误提前到进程启动，
        比让人事后去查“用户表为什么是空的”便宜得多。
        """
        cleaned = value.strip()
        if not cleaned.startswith("postgresql+asyncpg://"):
            raise ValueError(
                f"DATABASE_URL 必须是 postgresql+asyncpg://…，收到 {cleaned.split('://')[0]}://…。"
                "SQLite 已不再支持：请改用 Postgres（本地可用 `docker compose up -d postgres`）。"
            )
        return cleaned

    @field_validator("s3_bucket")
    @classmethod
    def _validate_bucket_name(cls, value: str) -> str:
        """桶名必须是 S3 兼容的小写 DNS 名。

        带大写/下划线的桶名在 AWS S3 上会被建桶请求直接拒绝，在自建网关上的行为则
        看实现——属于“只在某些环境炸”的配置错误，因此在启动时就归一化检查。
        """
        cleaned = value.strip()
        if not S3_BUCKET_PATTERN.match(cleaned):
            raise ValueError(
                f"S3_BUCKET={cleaned!r} 不是合法的桶名：只允许 3-63 位小写字母/数字/连字符/点，"
                "且不能以点或连字符开头结尾。"
            )
        return cleaned

    @field_validator("upload_allowed_extensions")
    @classmethod
    def _normalise_extensions(cls, value: str) -> str:
        """扩展名白名单归一化：去点、小写、去重、去空。

        配置里写 `.PDF` 或 `pdf, pdf` 是常见笔误。若不归一化，白名单比较会变成
        “看你怎么写的”——同一个文件在不同部署上时好时坏。
        """
        seen: list[str] = []
        for item in value.split(","):
            cleaned = item.strip().lower().lstrip(".")
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        if not seen:
            raise ValueError("UPLOAD_ALLOWED_EXTENSIONS 不能为空：至少要允许一种可解析的文本格式。")
        return ",".join(seen)

    @model_validator(mode="after")
    def _validate_upload_limits(self) -> "Settings":
        """上传上限必须小于全局请求体上限，否则文件永远传不进来。

        两个限额分别由 `MaxBodySizeMiddleware`（整个请求体）与流式计数（文件本身）执行。
        配反了的表现极具误导性：用户上传一个 20MB 的文件，收到的是
        「请求体过大」而不是「文件过大」，而后端日志里根本看不到这次上传——它在进
        应用之前就被中间件截断了。
        """
        if self.uploads_enabled:
            # multipart 边界与表单字段的额外开销约几百字节，留 1MB 余量
            headroom = 1 * 1024 * 1024
            if self.max_request_body_bytes < self.upload_max_bytes + headroom:
                raise ValueError(
                    f"MAX_REQUEST_BODY_BYTES（{self.max_request_body_bytes:,} 字节）必须至少比 "
                    f"UPLOAD_MAX_BYTES（{self.upload_max_bytes:,} 字节）大 1MB，"
                    "否则中间件会在文件进入上传逻辑之前就返回 413（用户看到的是「请求体过大」，"
                    "而后端日志里看不到这次上传）。"
                )
        return self

    def is_upload_extension_allowed(self, filename: str) -> bool:
        """文件名扩展名是否在白名单内（大小写不敏感）。

        只看扩展名，不代替 MIME 嗅探：两者任一不匹配都拒绝，见
        `app/services/uploads/validators.py`。
        """
        suffix = Path(filename or "").suffix.lower().lstrip(".")
        return bool(suffix) and suffix in set(self.upload_allowed_extensions.split(","))

    @field_validator("app_env", mode="before")
    @classmethod
    def _normalise_app_env(cls, value: object) -> object:
        """归一化 APP_ENV，并拒绝白名单外的取值。

        生产守卫与 .env.{env} 的选择都拿 app_env 做字符串比较，原始值一旦漏进来，
        `APP_ENV=Production` 就会绕过 `== "production"` 的判断；拼错的环境名还会
        静默加载不到对应的 .env 文件。归一化与校验都必须发生在赋值之前。
        """
        if not isinstance(value, str):
            return value
        normalised = value.strip().lower()
        if normalised not in ALLOWED_APP_ENVS:
            allowed = "、".join(sorted(ALLOWED_APP_ENVS))
            raise ValueError(
                f"APP_ENV 必须是 {allowed} 之一（当前值：{normalised!r}），拼错会导致 .env 文件静默不加载。"
            )
        return normalised

    @model_validator(mode="after")
    def _reject_placeholder_secret_in_production(self) -> "Settings":
        """生产环境用默认密钥等于没有密钥：任何人都能伪造重置密码令牌。

        选择启动即报错而不是打日志警告——警告会被忽略，而这个问题的影响是
        任何人都能接管任意账号。
        """
        if self.app_env == "production" and _is_weak_auth_secret(self.auth_secret):
            raise ValueError(
                "生产环境必须设置强随机 AUTH_SECRET：不能是默认/占位值，且至少 32 个字符。"
                "生成方式：python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return self

    @model_validator(mode="after")
    def _reject_insecure_cookie_in_production(self) -> "Settings":
        """生产必须走 HTTPS cookie：明文 HTTP 下会话 token 可被链路上任意一跳直接截获。"""
        if self.app_env == "production" and not self.auth_cookie_secure:
            raise ValueError("生产环境必须设置 AUTH_COOKIE_SECURE=true，否则会话 cookie 会在明文 HTTP 上被截获。")
        return self

    @model_validator(mode="after")
    def _validate_cookie_name_prefix(self) -> "Settings":
        """`__Host-` / `__Secure-` 前缀有硬性属性要求，不满足时浏览器**静默丢弃** cookie。

        这两类前缀是 RFC 6265bis 的「cookie 作用域锁定」：带 `__Host-` 的 cookie 必须同时
        有 `Secure`、`Path=/`、且**没有 Domain**，浏览器才肯存。运维看到的是「登录接口
        204、响应里也确实有 Set-Cookie」，然后下一个请求 401——最难查的那一类。

        所以名称与前缀属性一起校验，启动即时报错并写清修法。

        另：cookie 名必须是 RFC 6265 的 token（不能有空格、分号、逗号等）。带这些字符时
        浏览器/代理的行为是未定义的，同样属于「配错了但看不出来」。
        """
        name = self.auth_cookie_name
        if not COOKIE_NAME_PATTERN.match(name):
            raise ValueError(
                f"AUTH_COOKIE_NAME={name!r} 不是合法的 cookie 名：只允许字母、数字与 "
                "!#$%&'*+-.^_`|~，不能包含空格、分号、逗号或中文。"
            )
        if name.startswith("__Host-") and not self.auth_cookie_secure:
            raise ValueError(
                "AUTH_COOKIE_NAME 用了 `__Host-` 前缀，浏览器要求这类 cookie 必须带 Secure，"
                "否则会被直接丢弃（表现为「登录成功，但下一个请求 401」）。"
                "请同时设置 AUTH_COOKIE_SECURE=true，或改用不带前缀的名字。"
            )
        if name.startswith("__Secure-") and not self.auth_cookie_secure:
            raise ValueError(
                "AUTH_COOKIE_NAME 用了 `__Secure-` 前缀，浏览器要求这类 cookie 必须带 Secure，"
                "否则会被直接丢弃。请同时设置 AUTH_COOKIE_SECURE=true，或改用不带前缀的名字。"
            )
        return self

    def resolved_cookie_samesite(self) -> Literal["lax", "strict", "none"]:
        """实际生效的 SameSite 取值。

        跨域部署（`cors_allow_credentials=true`）必须用 `none`：`lax` 只在同站导航时
        回传，跨站 `fetch` 一律不带 cookie，表现为「登录成功、下一个请求 401」。
        显式配置优先，方便同站部署想收紧到 `strict` 的场景。
        """
        if self.auth_cookie_samesite is not None:
            return self.auth_cookie_samesite
        return "none" if self.cors_allow_credentials else "lax"

    @model_validator(mode="after")
    def _reject_samesite_none_without_secure(self) -> "Settings":
        """`SameSite=None` 必须配 `Secure`：否则浏览器直接丢弃 cookie。

        这个组合的失败方式特别难查——登录接口返回 204、响应里也确实带了 Set-Cookie，
        但下一个请求就是 401，看起来像「会话没存住」。启动即报错比线上猜半天好。
        """
        if self.resolved_cookie_samesite() == "none" and not self.auth_cookie_secure:
            raise ValueError(
                "会话 cookie 的 SameSite=None 必须同时设置 AUTH_COOKIE_SECURE=true："
                "浏览器会丢弃「SameSite=None 但没有 Secure」的 cookie，"
                "表现为登录成功但后续请求全部 401。"
                "跨站部署请开 HTTPS 并设 AUTH_COOKIE_SECURE=true；"
                "若前后端同站，请把 CORS_ALLOW_CREDENTIALS 设为 false 或显式设 AUTH_COOKIE_SAMESITE=lax。"
            )
        return self

    def get_cors_origins(self) -> list[str]:
        """解析 CORS 来源列表。

        留空时**只有非 production** 回退本地前端端口。此前默认值直接写死两个
        localhost，生产环境忘了配置就会允许本地页面带凭据读生产 API；同源部署不需要
        CORS，让 production 留空返回空列表更安全。

        同时把常见的配置笔误收掉：尾斜杠（浏览器 Origin 从不带斜杠）、重复项、
        `http://` 这种只剩协议的坏值；保留 `*`。
        """
        default_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
        raw = (self.cors_allowed_origins or "").strip()
        if not raw:
            return default_origins if self.app_env != "production" else []

        origins: list[str] = []
        for item in raw.split(","):
            origin = item.strip().rstrip("/")
            # 只剩协议（http://）或空项不是合法 Origin；静默跳过比放一个永远匹配不上的值好
            if not origin or origin.endswith(":"):
                continue
            if origin not in origins:
                origins.append(origin)

        if origins:
            return origins
        return default_origins if self.app_env != "production" else []

    model_config = SettingsConfigDict(
        env_file=get_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        # 校验失败时 pydantic 默认把整个输入字典塞进异常/日志，其中含 AUTH_SECRET、
        # LITELLM_API_KEY、TAVILY_API_KEY；隐藏 input 只保留字段名与原因。
        hide_input_in_errors=True,
    )


settings = Settings()

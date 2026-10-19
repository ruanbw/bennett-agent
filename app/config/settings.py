import os
import re
from pathlib import Path

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
            "上游模型条目里用作展示描述的字段名。改动前这个字段名硬编码在"
            "model_service 里，让一个自称网关无关的模块耦合到了特定网关；"
            "缺失时回退为 'provider name'。"
        ),
    )
    assistant_name: str = Field(default="Bennett", description="助手名称")
    tavily_api_key: str | None = Field(default=None, description="Tavily 网页搜索 API Key")
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="逗号分隔的 CORS 允许来源，默认开发端口；设为 * 时允许任意源",
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
    trusted_proxy_headers: str = Field(
        default="",
        description=(
            "逗号分隔的受信任反向代理 IP 标头列表（如 'X-Forwarded-For,X-Real-IP'）。"
            "仅当部署在受信反向代理之后时配置，取最右非空值（要求代理覆盖或追加）；"
            "为空时直接使用直连 client.host，防止客户端伪造标头绕过限流。"
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

    # ------------------------------------------------------------------
    # 数据库与认证
    # ------------------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./bennett.db",
        description=(
            "SQLAlchemy 异步连接串。默认 SQLite 落在工作目录，单机部署够用；"
            "注意 SQLite 文件不能放在 NFS 等网络文件系统上，会损坏。"
        ),
    )
    auth_secret: str = Field(
        default=DEFAULT_AUTH_SECRET,
        description="重置密码/邮箱验证令牌的签名密钥。生产必须替换为 ≥32 字符的强随机串。",
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
    session_lifetime_seconds: int = Field(
        default=30 * 24 * 60 * 60,
        ge=60,
        description=(
            "登录态有效期（秒），默认 30 天，下限 60：0 会让 fastapi-users 认为「不设过期」"
            "（服务端永不过期），而 cookie Max-Age=0 又会立即删除 cookie，语义互相矛盾。"
        ),
    )

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

    def get_cors_origins(self) -> list[str]:
        default_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
        if not self.cors_allowed_origins or not self.cors_allowed_origins.strip():
            return default_origins
        origins = [item.strip() for item in self.cors_allowed_origins.split(",") if item.strip()]
        return origins or default_origins

    model_config = SettingsConfigDict(
        env_file=get_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        # 校验失败时 pydantic 默认把整个输入字典塞进异常/日志，其中含 AUTH_SECRET、
        # LITELLM_API_KEY、TAVILY_API_KEY；隐藏 input 只保留字段名与原因。
        hide_input_in_errors=True,
    )


settings = Settings()

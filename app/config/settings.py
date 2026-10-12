import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

DEFAULT_APP_ENV = "development"

#: 认证密钥的占位默认值。生产环境若仍是这个值，启动时直接报错而不是静默放行。
DEFAULT_AUTH_SECRET = "dev-insecure-secret-change-me"

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

    app_env: str = Field(default=DEFAULT_APP_ENV, description="运行环境")
    litellm_api_base: str = Field(default="http://localhost:4000", description="LiteLLM Proxy API 基础地址")
    litellm_api_key: str | None = Field(default=None, description="LiteLLM API 密钥")
    model_name: str = Field(default="openai/gpt-4o", description="模型标识符（LiteLLM 路由格式）")
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
    chat_rate_limit_per_minute: int = Field(
        default=30,
        description="单个**账号**每分钟可发起的对话/标题请求数；0 表示不限流。",
    )
    auth_rate_limit_per_minute: int = Field(
        default=10,
        description=(
            "单个**来源 IP** 每分钟可发起的登录/注册请求数；0 表示不限流。"
            "挡的是密码暴力破解与批量注册。"
        ),
    )
    trusted_proxy_headers: str = Field(
        default="",
        description=(
            "逗号分隔的受信任反向代理 IP 标头列表（如 'X-Forwarded-For,X-Real-IP'）。"
            "仅当部署在受信反向代理之后时配置，按顺序提取第一个非空客户端 IP；"
            "为空时直接使用直连 client.host，防止客户端伪造标头绕过限流。"
        ),
    )
    model_upstream_timeout: float = Field(
        default=120.0,
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
        description="重置密码/邮箱验证令牌的签名密钥。生产必须替换为强随机串。",
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
        description="登录态有效期（秒），默认 30 天。改小会让所有会话提前失效。",
    )

    @model_validator(mode="after")
    def _reject_placeholder_secret_in_production(self) -> "Settings":
        """生产环境用默认密钥等于没有密钥：任何人都能伪造重置密码令牌。

        选择启动即报错而不是打日志警告——警告会被忽略，而这个问题的影响是"
        "任何人都能接管任意账号。
        """
        if self.app_env == "production" and self.auth_secret == DEFAULT_AUTH_SECRET:
            raise ValueError(
                "生产环境必须设置 AUTH_SECRET（当前仍是默认占位值）。"
                "生成方式：python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
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
    )


settings = Settings()

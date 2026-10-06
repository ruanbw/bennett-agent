import os
from pathlib import Path
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def get_env_files() -> tuple[Path, ...]:
    """根据 APP_ENV 动态解析环境变量文件加载顺序。

    优先级：系统环境变量 > .env.{APP_ENV}.local > .env.{APP_ENV} > .env.local > .env
    """
    env = os.getenv("APP_ENV", "development").lower()
    env_files: List[Path] = [
        BASE_DIR / ".env",
        BASE_DIR / ".env.local",
        BASE_DIR / f".env.{env}",
        BASE_DIR / f".env.{env}.local",
    ]
    return tuple(f for f in env_files if f.is_file())


class Settings(BaseSettings):
    """全局应用配置"""
    app_env: str = Field(default="development", description="运行环境")
    litellm_api_base: str = Field(default="http://localhost:4000", description="LiteLLM Proxy API 基础地址")
    litellm_api_key: str = Field(default="sk-placeholder", description="LiteLLM API 密钥")
    model_name: str = Field(default="openai/gpt-4o", description="模型标识符（LiteLLM 路由格式）")
    assistant_name: str = Field(default="Bennett", description="助手名称")
    tavily_api_key: Optional[str] = Field(default=None, description="Tavily 网页搜索 API Key")

    model_config = SettingsConfigDict(
        env_file=get_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

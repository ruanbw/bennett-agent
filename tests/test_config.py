"""C-1 的回归锁：APP_ENV 的选择必须同时支持 shell 与 dotenv 文件。

修复前的行为（已在 CODE_REVIEW.md C-1 记录）：
`get_env_files()` 用 `os.getenv("APP_ENV", "development")` 判定环境，而此时
pydantic-settings 尚未加载任何 dotenv 文件。后果有两层——

1. 写进 `.env` 的 `APP_ENV=staging` 完全无法选择环境；
2. 更糟的是 `.env.development` 会被顺带加载，它自带的 `APP_ENV=development`
   位于加载序列靠后位置（优先级更高），于是反过来覆盖运维的意图，且全程静默。

下面的用例覆盖进程环境变量、`.env.local`、`.env` 三条来源与加载顺序。
"""

import pytest
from pydantic import ValidationError

from app.config.settings import (
    DEFAULT_APP_ENV,
    DEFAULT_AUTH_SECRET,
    Settings,
    get_env_files,
    resolve_app_env,
)


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """隔离的「项目根」，同时清掉进程里的 APP_ENV 干扰。"""
    monkeypatch.delenv("APP_ENV", raising=False)
    return tmp_path


def write_env_file(base, name, app_env=None):
    lines = ["# 测试夹具"]
    if app_env is not None:
        lines.append(f"APP_ENV={app_env}")
    (base / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def file_names(files):
    return [f.name for f in files]


# ---------------------------------------------------------------------
# resolve_app_env
# ---------------------------------------------------------------------
def test_defaults_to_development_without_any_config(project_dir):
    assert resolve_app_env(project_dir) == DEFAULT_APP_ENV


def test_process_env_wins_over_dotenv(project_dir, monkeypatch):
    write_env_file(project_dir, ".env", app_env="staging")
    monkeypatch.setenv("APP_ENV", "production")
    assert resolve_app_env(project_dir) == "production"


@pytest.mark.parametrize("raw,expected", [("PRODUCTION", "production"), ("  Staging  ", "staging")])
def test_env_name_is_normalised(project_dir, monkeypatch, raw, expected):
    monkeypatch.setenv("APP_ENV", raw)
    assert resolve_app_env(project_dir) == expected


def test_app_env_can_come_from_dotenv(project_dir):
    """修复前此用例返回 'development'。"""
    write_env_file(project_dir, ".env", app_env="staging")
    assert resolve_app_env(project_dir) == "staging"


def test_env_local_beats_env(project_dir):
    write_env_file(project_dir, ".env", app_env="staging")
    write_env_file(project_dir, ".env.local", app_env="production")
    assert resolve_app_env(project_dir) == "production"


def test_env_name_is_not_read_from_env_specific_files(project_dir):
    """.env.{env} 的文件名由 APP_ENV 决定，不能参与判定它自身，否则形成循环依赖。"""
    write_env_file(project_dir, ".env.development", app_env="development")
    write_env_file(project_dir, ".env.production", app_env="production")
    assert resolve_app_env(project_dir) == DEFAULT_APP_ENV


def test_blank_app_env_in_file_is_ignored(project_dir):
    write_env_file(project_dir, ".env", app_env="")
    assert resolve_app_env(project_dir) == DEFAULT_APP_ENV


# ---------------------------------------------------------------------
# get_env_files
# ---------------------------------------------------------------------
def test_env_specific_file_is_not_loaded_for_another_env(project_dir):
    """核心回归：APP_ENV=staging 时不得加载 .env.development。

    修复前 env_files 为 ['.env', '.env.development']，靠后者优先级更高，
    于是 .env.development 里的 APP_ENV=development 反压了 .env 的 staging。
    """
    write_env_file(project_dir, ".env", app_env="staging")
    write_env_file(project_dir, ".env.development", app_env="development")

    assert file_names(get_env_files(project_dir)) == [".env"]


def test_load_order_is_low_to_high_priority(project_dir):
    """pydantic-settings 对 env_file 元组按「靠后者覆盖靠前者」处理。"""
    write_env_file(project_dir, ".env", app_env="staging")
    write_env_file(project_dir, ".env.local")
    write_env_file(project_dir, ".env.staging")
    write_env_file(project_dir, ".env.staging.local")

    assert file_names(get_env_files(project_dir)) == [
        ".env",
        ".env.local",
        ".env.staging",
        ".env.staging.local",
    ]


def test_missing_files_are_skipped(project_dir):
    write_env_file(project_dir, ".env", app_env="staging")
    assert file_names(get_env_files(project_dir)) == [".env"]


def test_empty_directory_yields_no_files(project_dir):
    assert get_env_files(project_dir) == ()


# ---------------------------------------------------------------------
# 认证密钥守卫
# ---------------------------------------------------------------------
def test_production_rejects_placeholder_auth_secret():
    """生产环境用默认密钥等于没有密钥：任何人都能伪造重置密码令牌。

    这里选择启动即报错而不是打日志警告——警告会被忽略，而后果是任意账号可被接管。
    """
    with pytest.raises(ValidationError):
        Settings(app_env="production", auth_secret=DEFAULT_AUTH_SECRET, _env_file=None)


def test_production_accepts_a_real_secret():
    config = Settings(app_env="production", auth_secret="a-real-secret", _env_file=None)

    assert config.app_env == "production"


def test_development_allows_placeholder_secret():
    """守卫只针对生产：否则本地起服务都要先配密钥，开发体验没必要这么重。"""
    config = Settings(app_env="development", auth_secret=DEFAULT_AUTH_SECRET, _env_file=None)

    assert config.auth_secret == DEFAULT_AUTH_SECRET

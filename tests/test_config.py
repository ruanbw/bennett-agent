"""C-1 的回归锁：APP_ENV 的选择必须同时支持 shell 与 dotenv 文件。

修复前的行为（已在 CODE_REVIEW.md C-1 记录）：
`get_env_files()` 用 `os.getenv("APP_ENV", "development")` 判定环境，而此时
pydantic-settings 尚未加载任何 dotenv 文件。后果有两层——

1. 写进 `.env` 的 `APP_ENV=staging` 完全无法选择环境；
2. 更糟的是 `.env.development` 会被顺带加载，它自带的 `APP_ENV=development`
   位于加载序列靠后位置（优先级更高），于是反过来覆盖运维的意图，且全程静默。

下面的用例覆盖进程环境变量、`.env.local`、`.env` 三条来源与加载顺序。
本文件同时锁住配置守卫的新语义：app_env 归一化与白名单、生产强密钥、生产 Secure
cookie、数值下限、CORS 解析，以及校验失败时不把密钥输入打进异常文本。
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


def test_env_specific_file_lookup_lowercases_app_env(project_dir, monkeypatch):
    """APP_ENV=Production 也必须能命中 .env.production 文件。"""
    monkeypatch.setenv("APP_ENV", "Production")
    write_env_file(project_dir, ".env.production")

    assert file_names(get_env_files(project_dir)) == [".env.production"]


def test_settings_applies_env_file_order(project_dir):
    """env_file 元组靠后者覆盖靠前者：.env.staging 必须压过 .env。"""
    (project_dir / ".env").write_text("APP_ENV=staging\nASSISTANT_NAME=from-base\n", encoding="utf-8")
    (project_dir / ".env.staging").write_text("ASSISTANT_NAME=from-staging\n", encoding="utf-8")

    config = Settings(_env_file=get_env_files(project_dir))

    assert config.app_env == "staging"
    assert config.assistant_name == "from-staging"


# ---------------------------------------------------------------------
# app_env 归一化与白名单
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [("  STAGING  ", "staging"), ("Test", "test"), ("DEVELOPMENT", "development")],
)
def test_app_env_is_stripped_and_lowercased(raw, expected):
    assert Settings(app_env=raw, _env_file=None).app_env == expected


def test_production_app_env_is_normalised_before_guard():
    """APP_ENV=Production 必须与守卫里的 'production' 等价（修复前会静默绕过）。"""
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_env="  Production  ",
            auth_secret=DEFAULT_AUTH_SECRET,
            auth_cookie_secure=True,
            _env_file=None,
        )

    assert "AUTH_SECRET" in str(exc.value)


@pytest.mark.parametrize("raw", ["prod", "prd", "Productionn", "", "  "])
def test_unknown_app_env_is_rejected(raw):
    """拼错的环境名会让 .env.{env} 静默不加载，必须启动即报错。"""
    with pytest.raises(ValidationError) as exc:
        Settings(app_env=raw, _env_file=None)

    assert "app_env" in str(exc.value)
    assert "development" in str(exc.value)  # 错误信息列出白名单


# ---------------------------------------------------------------------
# 认证密钥守卫
# ---------------------------------------------------------------------
#: 38 字符、不含任何占位词的生产级测试密钥（仅测试用，不要复用）。
STRONG_TEST_SECRET = "Zx9Qm2Lp7Rv4Tn8Wc3Hk6Bs1Yd5Gj0Fa2Me4Uu"


def test_production_rejects_placeholder_auth_secret():
    """生产环境用默认密钥等于没有密钥：任何人都能伪造重置密码令牌。

    这里选择启动即报错而不是打日志警告——警告会被忽略，而后果是任意账号可被接管。
    """
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_env="production",
            auth_secret=DEFAULT_AUTH_SECRET,
            auth_cookie_secure=True,
            _env_file=None,
        )

    assert "AUTH_SECRET" in str(exc.value)


@pytest.mark.parametrize(
    "secret",
    [
        "replace-with-a-strong-random-value",
        "CHANGE-ME-PLEASE-DO-IT-NOW-1234567890",
        "your-own-secret-value-here-1234567890abc",
        "example-secret-value-1234567890abcdefgh",
        "placeholder-secret-1234567890abcdefghij",
        "insecure-secret-1234567890abcdefghijkl",
    ],
)
def test_production_rejects_placeholder_words_case_insensitively(secret):
    """这些串都 ≥32 字符，失败原因只能是占位词而不是长度。"""
    with pytest.raises(ValidationError) as exc:
        Settings(app_env="production", auth_secret=secret, auth_cookie_secure=True, _env_file=None)

    assert "AUTH_SECRET" in str(exc.value)


@pytest.mark.parametrize("secret", ["only-31-characters-secret-12345", "short"])
def test_production_rejects_short_auth_secret(secret):
    with pytest.raises(ValidationError) as exc:
        Settings(app_env="production", auth_secret=secret, auth_cookie_secure=True, _env_file=None)

    assert "AUTH_SECRET" in str(exc.value)


def test_production_accepts_secret_at_minimum_length():
    """下限是 32：恰好 32 个字符必须放行（把 < 写成 <= 会被这条挡住）。"""
    secret = "abcdefghijklmnopqrstuvwxyz012345"
    assert len(secret) == 32

    config = Settings(app_env="production", auth_secret=secret, auth_cookie_secure=True, _env_file=None)

    assert config.auth_secret == secret


def test_production_accepts_a_real_secret():
    config = Settings(
        app_env="production",
        auth_secret=STRONG_TEST_SECRET,
        auth_cookie_secure=True,
        _env_file=None,
    )

    assert config.app_env == "production"


def test_development_allows_placeholder_secret():
    """守卫只针对生产：否则本地起服务都要先配密钥，开发体验没必要这么重。"""
    config = Settings(app_env="development", auth_secret=DEFAULT_AUTH_SECRET, _env_file=None)

    assert config.auth_secret == DEFAULT_AUTH_SECRET


def test_validation_errors_do_not_dump_secret_input():
    """校验失败时异常文本会进日志，不能把整个输入字典（含各类密钥）带出去。"""
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_env="production",
            auth_secret=DEFAULT_AUTH_SECRET,
            litellm_api_key="sk-live-LEAK-CANARY-1234567890",
            tavily_api_key="tvly-LEAK-CANARY-1234567890",
            auth_cookie_secure=True,
            _env_file=None,
        )

    rendered = str(exc.value)

    assert "input_value" not in rendered
    assert DEFAULT_AUTH_SECRET not in rendered
    assert "LEAK-CANARY" not in rendered


# ---------------------------------------------------------------------
# 生产 cookie 守卫
# ---------------------------------------------------------------------
def test_production_rejects_insecure_cookie():
    """明文 HTTP 下会话 cookie 可被链路上任意一跳截获。"""
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_env="production",
            auth_secret=STRONG_TEST_SECRET,
            auth_cookie_secure=False,
            _env_file=None,
        )

    assert "AUTH_COOKIE_SECURE" in str(exc.value)


def test_development_allows_insecure_cookie():
    """本地 http 调试必须允许 false，否则浏览器不回传 cookie。"""
    config = Settings(app_env="development", auth_cookie_secure=False, _env_file=None)

    assert config.auth_cookie_secure is False


# ---------------------------------------------------------------------
# 数值范围
# ---------------------------------------------------------------------
@pytest.mark.parametrize("value", [-10, 0, 59])
def test_session_lifetime_below_minimum_is_rejected(value):
    """0 会让服务端会话「永不过期」而 cookie 立即删除，语义相反，必须拒绝。"""
    with pytest.raises(ValidationError) as exc:
        Settings(session_lifetime_seconds=value, _env_file=None)

    assert "session_lifetime_seconds" in str(exc.value)


def test_session_lifetime_minimum_is_accepted():
    assert Settings(session_lifetime_seconds=60, _env_file=None).session_lifetime_seconds == 60


@pytest.mark.parametrize("field", ["chat_rate_limit_per_minute", "auth_rate_limit_per_minute"])
def test_rate_limit_rejects_negative_values(field):
    with pytest.raises(ValidationError) as exc:
        Settings(**{field: -1}, _env_file=None)

    assert field in str(exc.value)


@pytest.mark.parametrize("field", ["chat_rate_limit_per_minute", "auth_rate_limit_per_minute"])
def test_zero_rate_limit_still_means_disabled(field):
    """0=关闭限流是既有语义（app/api/rate_limit.py 依赖它），不能被下限改掉。"""
    assert getattr(Settings(**{field: 0}, _env_file=None), field) == 0


@pytest.mark.parametrize("value", [-1, -0.5])
def test_model_upstream_timeout_rejects_negative_values(value):
    with pytest.raises(ValidationError) as exc:
        Settings(model_upstream_timeout=value, _env_file=None)

    assert "model_upstream_timeout" in str(exc.value)


def test_model_upstream_timeout_zero_means_unlimited():
    assert Settings(model_upstream_timeout=0, _env_file=None).model_upstream_timeout == 0


# ---------------------------------------------------------------------
# CORS 解析与新字段
# ---------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["", "   ", ",,", " , "])
def test_cors_origins_blank_falls_back_to_defaults(raw):
    assert Settings(cors_allowed_origins=raw, _env_file=None).get_cors_origins() == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_cors_origins_trims_whitespace_and_drops_empty_entries():
    config = Settings(cors_allowed_origins=" http://a.example ,, http://b.example ", _env_file=None)

    assert config.get_cors_origins() == ["http://a.example", "http://b.example"]


def test_cors_origins_wildcard_is_preserved():
    assert Settings(cors_allowed_origins="*", _env_file=None).get_cors_origins() == ["*"]


def test_new_fields_have_expected_defaults():
    config = Settings(_env_file=None)

    assert config.cors_allow_credentials is False
    assert config.enable_api_docs is None  # None=自动：production 关闭，其余环境开启


def test_new_fields_accept_explicit_overrides():
    config = Settings(cors_allow_credentials=True, enable_api_docs=False, _env_file=None)

    assert config.cors_allow_credentials is True
    assert config.enable_api_docs is False

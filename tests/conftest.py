"""测试全局配置。

**必须在导入任何 app 模块之前**把数据库指向临时文件。原因：`app/db.py` 在导入时
就构造 engine，而默认的 `sqlite+aiosqlite:///./bennett.db` 会被 `_resolve_database_url`
锚定到**仓库根目录**——不提前覆盖 `DATABASE_URL` 的话，测试会直接读写开发用的
bennett.db（真实账号与会话都在里面），并留下 -wal / -shm 副产品。

写在模块级（而不是 fixture 里）是刻意的：fixture 执行时 app 模块通常已被导入，
engine 早已按默认路径建好了，此时再改环境变量为时已晚。临时目录的删除放在
session 级 fixture：它晚于本会话所有用例执行，顺序比 atexit 更明确。
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

#: 整个测试会话共用一个临时库。用例之间靠**唯一邮箱**隔离，而不是清库——
#: 重建 engine 需要动 `app.db` 的模块级单例，代价大于收益。
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="bennett-test-db-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB_DIR}/test.db"

# 关闭「启动时用环境变量引导默认提供商」。
#
# 测试里大多数模型用例针对的是**旧版网关路径**（mock `/v1/models` 后断言解析/兜底/
# 缓存），而 TestClient 启动应用会执行引导，库里一旦有提供商，`model_service` 就切到
# 数据库权威分支，那些用例会莫名其妙地只看得到 .env 里的默认模型。
# 需要引导行为的用例（管理后台）显式往库里写提供商；引导本身另有单元测试。
os.environ["BOOTSTRAP_PROVIDER_FROM_ENV"] = "false"

# 注册现在是邀请码制：未配置邀请码时注册接口一律 403。绝大多数用例都要真注册账号，
# 这里给整个会话配一个固定邀请码（各测试文件从 `settings.registration_invite_code`
# 读取，不硬编码）。需要验证「注册关闭」的用例自己 monkeypatch 该字段为空。
os.environ["REGISTRATION_INVITE_CODE"] = "test-invite-code"


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db_dir():
    """会话结束后删除临时库目录。

    `ignore_errors=True`：个别平台可能仍持有文件句柄，清理失败不该让测试结果变红；
    目录位于系统临时区，最坏情况由系统自行回收。
    """
    yield
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _create_test_schema():
    """整个会话开始前建表。

    模型配置服务现在每个请求都会查 `model_provider` 表（有配置时数据库就是权威）。
    不做这一步，直接调用 `fetch_available_models()` 的用例会拿到
    `no such table: model_provider`，而不再是旧版的网关兜底。
    """
    import asyncio

    from app.db import create_db_and_tables

    asyncio.run(create_db_and_tables())
    yield


async def _clear_model_registry() -> None:
    """清空提供商/模型表并重置装配缓存。

    这些是**跨用例共享的数据库状态**：任何用 TestClient 启动应用的用例都会触发
    「首次启动引导默认提供商」，于是后续依赖旧版网关路径的用例会突然看到数据库
    权威分支。按用例清空比给每个用例写清理代码可靠。
    """
    from sqlalchemy import delete

    from app.db import AppSetting, ModelProvider, ProviderModel, async_session_maker
    from app.services.agent_factory import reset_agent_cache
    from app.services.model_service import REGISTRY_MARKER_KEY, reset_models_cache

    async with async_session_maker() as session:
        await session.execute(delete(ProviderModel))
        await session.execute(delete(ModelProvider))
        # 初始化标记也要清：否则「引导过一次」之后的用例会永久走数据库权威分支。
        # 只删这一行：app_setting 是通用键值表，整表清空会连未来新增的应用级设置一起
        # 静默清掉，测试与线上行为分叉。
        await session.execute(delete(AppSetting).where(AppSetting.key == REGISTRY_MARKER_KEY))
        await session.commit()

    reset_models_cache()
    reset_agent_cache()


@pytest.fixture(autouse=True)
async def _reset_model_registry():
    """每个用例前后都从空注册表开始（见 `_clear_model_registry`）。"""
    await _clear_model_registry()
    yield
    await _clear_model_registry()


def _reset_limiter(module: object, attr: str) -> None:
    """把模块级限流器置回 None，属性不存在时直接失败。

    `chat_api._limiter = None` 这种写法在属性被改名后会**静默新建**一个属性，真正
    要重置的旧限流器原封不动，于是又回到「单跑通过、全量失败」。先检查属性是否存在，
    改名时立刻报错而不是留下隐患。
    """
    if not hasattr(module, attr):
        name = getattr(module, "__name__", repr(module))
        raise AssertionError(f"{name} 缺少 {attr!r}：限流器被改名/移除，请同步更新 tests/conftest.py")
    setattr(module, attr, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """重置进程内限流器。

    两个限流桶都是**模块级状态**，会跨用例累积。而认证端点按 IP 限流、所有测试都
    来自同一个 IP（testserver），不重置的话用例会互相挤占配额——表现为「单跑通过、
    全量跑失败」这类最难定位的失败。

    放在 conftest 而不是某个测试文件里：限流是跨文件生效的全局副作用，任何新增的
    测试文件都该自动受到保护。
    """
    from app.api import chat as chat_api
    from app.api import rate_limit

    _reset_limiter(chat_api, "_limiter")
    _reset_limiter(rate_limit, "_auth_limiter")

    yield

    _reset_limiter(chat_api, "_limiter")
    _reset_limiter(rate_limit, "_auth_limiter")


@pytest.fixture(autouse=True)
def _stub_context_window(monkeypatch):
    """上下文预算预检默认走「网关没报窗口」这条放行路径。

    预检要查网关的模型列表（真实 HTTP），而开发机上 3425 很可能真的挂着一个网关：
    不隔离的话，同一份代码在有网关的机器上会按真实窗口开始拒绝历史较长的用例，
    表现为「本机能跑、CI 不能跑」或反之。需要预算真正生效的用例自己
    monkeypatch `token_budget.get_model_limits`（见 tests/test_token_budget.py 的
    `patch_limits`，或者 test_chat_service 里的 over 用例）。
    """
    from app.services import token_budget

    async def unknown_limits(_model_id):
        return None, None

    monkeypatch.setattr(token_budget, "get_model_limits", unknown_limits)

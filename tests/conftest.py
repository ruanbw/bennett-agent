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


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db_dir():
    """会话结束后删除临时库目录。

    `ignore_errors=True`：个别平台可能仍持有文件句柄，清理失败不该让测试结果变红；
    目录位于系统临时区，最坏情况由系统自行回收。
    """
    yield
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


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

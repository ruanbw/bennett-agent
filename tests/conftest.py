"""测试全局配置。

**必须在导入任何 app 模块之前**把数据库指向临时文件。原因：`app/db.py` 在导入时
就构造 engine，而默认的 `sqlite+aiosqlite:///./bennett.db` 是相对**当前工作目录**
解析的——直接跑测试会在仓库根目录留下真实的 bennett.db / -wal / -shm 三个文件。

写在模块级（而不是 fixture 里）是刻意的：fixture 执行时 app 模块通常已被导入，
engine 早已按默认路径建好了，此时再改环境变量为时已晚。
"""

import os
import tempfile
from pathlib import Path

import pytest

#: 整个测试会话共用一个临时库。用例之间靠**唯一邮箱**隔离，而不是清库——
#: 重建 engine 需要动 `app.db` 的模块级单例，代价大于收益。
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="bennett-test-db-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB_DIR}/test.db"


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

    chat_api._limiter = None
    rate_limit._auth_limiter = None

    yield

    chat_api._limiter = None
    rate_limit._auth_limiter = None

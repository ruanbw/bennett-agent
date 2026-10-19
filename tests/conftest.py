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

#: 整个测试会话共用一个临时库。当前套件是纯逻辑单测，不读共享库；这层重定向是
#: 安全网——个别用例（如启动迁移）会直接构造 engine，任何测试都不该碰到开发库。
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

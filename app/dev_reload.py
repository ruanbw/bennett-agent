"""开发期热重启：改了代码就自动重启后端，且**不改变启动路径**。

## 为什么不用 `uvicorn --reload`

`uvicorn --reload` 的子进程是用 `spawn` 起的（`uvicorn/_subprocess.py` 的
`get_subprocess`），它只 `import app.server:app`，**不会重新执行 `main.py`**。而本仓库
把三件启动动作刻意放在 `main.py` 里（理由见该文件的 `main()`）：

1. `configure_logging()`：日志格式带 `[request_id]`，并给 uvicorn 的三个 logger 换 handler；
2. `sanitize_no_proxy_env()`：清理会让**所有** httpx 客户端在构造阶段失败的 `no_proxy` 条目；
3. `proxy_headers=False` / `log_config=None` / `timeout_graceful_shutdown=5`。

用 `uvicorn --reload` 时这三件全丢。这不是推测：实测子进程日志会退化成
`INFO:     ...`（没有 `[request_id]`、没有日期、没有 logger 名），坏 `no_proxy` 不再被
清理，代理头信任模型也回到框架默认。于是「开发用 `--reload`、部署用 `main.py`」两边
的日志与网络行为不一致——而「本地复现不出线上问题」正是这么来的。

所以这里的做法是：**子进程就是完整的 `python main.py --web`**，supervisor 只负责
「看文件 → 发 SIGTERM → 起新进程」。代价是多一个进程，收益是开发与部署走同一条启动路径。

## 为什么必须监听提示词与 .env

这两类改动都**不会**被 `import` 重新加载：

- `prompts/*.jinja`：Jinja 的 `auto_reload` 确实让 `prompt_manager.render()` 立刻读到新
  内容（`PromptManager` 没关它），但系统提示词是在 `_build_agent_cached` 装配时就渲染好
  并固化进 `Agent` 的，而装配结果带 `lru_cache`。实测：改模板后模板层能看到新内容，
  但缓存命中的 agent 里仍是旧的 system_prompt。所以**必须重启**。
- `.env*`：`app/config/settings.py` 的 `settings = Settings()` 是模块级单例，只在 import
  时读一次，运行期不重读。

## 这个文件自己不在监听效果内

supervisor 跑在父进程里，所以改 `app/dev_reload.py` 本身不会让 supervisor 重新加载自己
（子进程重启了，父进程还是旧的）。改了它就手动重启一次命令。

## 一次重启会掐断在飞请求

子进程收到 SIGTERM 后走 uvicorn 的优雅关闭，而 `timeout_graceful_shutdown=5` 意味着最多
等 5 秒就取消在飞任务，而单轮 SSE 的预算是 600 秒（`chat_service.TOTAL_STREAM_BUDGET_SECONDS`）。
所以**开发时任何一次保存都会掐断正在生成的回答**——这是刻意的，不是 bug；本模块等
`DEFAULT_GRACEFUL_TIMEOUT`（比 5 秒略长）后仍不退出就 SIGKILL，不让一次保存卡死在这里。
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from watchfiles import watch

logger = logging.getLogger(__name__)

#: 仓库根。按 `__file__` 定位而不是 cwd：从别的目录启动时 cwd 会变。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 递归监听的目录（相对仓库根）。只列真正会被进程加载的代码。
WATCH_DIRS: tuple[str, ...] = ("app", "prompts")

#: 单独监听的文件（相对仓库根）。
WATCH_FILES: tuple[str, ...] = ("main.py",)

#: 会被 `PromptManager` 当成模板的后缀（与 `app/prompts/manager.py` 的
#: `TEMPLATE_SUFFIXES` 同源；那边加了新后缀这里也要跟）。
TEMPLATE_SUFFIXES: tuple[str, ...] = (".jinja", ".jinja2", ".j2", ".txt")

#: 文件变更多久之后才重启（毫秒）。编辑器的「原子保存」是临时文件 + rename，会在极短时间
#: 内产生多个事件；不防抖会连着重启两次，第二次把第一次的启动过程打断。
DEFAULT_DEBOUNCE_MS = 500

#: 等子进程优雅退出的上限（秒）。必须**大于** uvicorn 的 `timeout_graceful_shutdown=5`，
#: 否则会把还在正常收尾的进程直接 SIGKILL，SQLite 的 WAL 就没有机会被干净地合并回主库。
DEFAULT_GRACEFUL_TIMEOUT = 6.0


def should_reload(path: Path) -> bool:
    """这个文件的变化是否要触发重启（纯函数，便于单测）。

    只认三类：Python 源码、`prompts/` 下的模板、**settings 真的会加载的** `.env` 文件。
    其余一律忽略——最要紧的是数据库文件：`bennett.db-wal` 每个请求都在写，若被当成
    变更源就会变成「一有请求就重启」。
    """
    if path.name.startswith(".env"):
        # 只有 `get_env_files()` 列出的那几个真被 settings 读取。用 glob 会把
        # `.env.example` 也算进来——它只是模板，改它不该重启（也重启不出任何效果，
        # 那才是最坏的一类：看起来生效了，其实没有）。
        return path.resolve() in _loaded_env_files()

    suffix = path.suffix.lower()
    if suffix == ".py":
        return True
    if suffix in TEMPLATE_SUFFIXES:
        # `.txt` 只在 prompts/ 下算模板：别把任意文档当成提示词变更
        return path.is_relative_to(PROJECT_ROOT / "prompts")
    return False


def _loaded_env_files() -> set[Path]:
    """settings 实际会加载的环境文件（与 `app/config/settings.py` 的 `get_env_files` 同源）。

    直接复用那个函数而不是在这里重写一遍叠加规则：它按 `APP_ENV` 决定加载
    `.env.{env}` / `.env.{env}.local`，两处各写一份迟早会漂移，而漂移的表现是
    「改了 .env.development.local 却不重启」——最难联想到监听列表上的那类问题。
    """
    from app.config.settings import get_env_files

    return {path.resolve() for path in get_env_files()}


def watch_paths(extra: Sequence[str] = ()) -> list[Path]:
    """本次要监听的全部路径（只返回真实存在的）。

    `.env` 用 `get_env_files()` 的结果而不是 glob：那份清单就是 settings 实际读取的
    文件，多监听没有意义，少监听会漏掉 `.env.{env}.local`。

    `extra` 里的路径**不做过滤**（调用方显式要求的就是要监听的），但仍然过一遍存在性检查：
    `watch()` 拿到不存在的路径会直接抛异常。
    """
    candidates: list[Path] = [PROJECT_ROOT / directory for directory in WATCH_DIRS]
    candidates += [PROJECT_ROOT / name for name in WATCH_FILES]
    candidates += sorted(_loaded_env_files())
    candidates += [Path(item).expanduser().resolve() for item in extra]
    return [path for path in candidates if path.exists()]


def build_child_command(host: str, port: int) -> list[str]:
    """子进程命令：完整的 `main.py --web`，**不带** `--reload`。

    不带 `--reload` 是防止无限自递归（子进程若也开 reload，会再起一个 supervisor）。
    用 `sys.executable` 而不是裸 `python`：在 `uv run` 下它指向当前 venv，换了 shell
    的 PATH 也不会跑到另一套解释器上。
    """
    return [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--web",
        "--host",
        host,
        "--port",
        str(port),
    ]


def stop_child(process: subprocess.Popen[bytes], graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT) -> None:
    """先 SIGTERM 等它自己退，超时才 SIGKILL。

    已经退出的进程直接返回：子进程启动失败（端口被占、配置错）时它早就死了，此时再
    `terminate()` 会对一个已回收的 pid 发信号。
    """
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=graceful_timeout)
    except subprocess.TimeoutExpired:
        logger.warning(
            "子进程 %.1fs 内没有退出（可能有 SSE 长连接在飞），强制结束",
            graceful_timeout,
        )
        process.kill()
        process.wait()


def _install_stop_handlers(stop: threading.Event) -> None:
    """把 SIGINT / SIGTERM 变成「让 watch 循环退出」，而不是就地中断。

    不接管的话，Ctrl-C 会在 `watch()` 内部抛 `KeyboardInterrupt`，跳过 `finally` 里的
    子进程清理，留下一个占着端口的孤儿进程。
    """

    def _handle(signum: int, _frame: Any) -> None:  # noqa: ANN401 - signal 回调签名
        logger.info("收到信号 %s，准备退出…", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except ValueError:
            # 非主线程装不了信号处理器；这时只退化成「Ctrl-C 会抛 KeyboardInterrupt」，
            # 由 run_dev_reload 的 try/finally 兜住子进程清理。
            logger.debug("当前线程无法注册信号处理器 %s", sig)


def _select_change(_change: Any, path: str) -> bool:  # noqa: ANN401 - watchfiles 过滤器签名
    return should_reload(Path(path))


def run_dev_reload(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    extra_dirs: Sequence[str] = (),
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT,
) -> int:
    """跑 supervisor，返回重启次数（进程退出时才有值）。

    没有文件变化也不重启；子进程自己崩了（比如启动时报配置错）就停在那里等下一次保存，
    而不是立刻重拉——否则一个必崩的启动错误会把日志刷满并吃满 CPU。
    """
    paths = watch_paths(extra_dirs)
    command = build_child_command(host, port)

    # 用日志而不是 print：格式与子进程的应用日志一致（带时间/级别/logger 名），
    # 混在同一个终端里时，supervisor 与子进程两行一眼能分开。
    logger.info("supervisor PID %d，子进程: %s", _pid(), " ".join(command))
    logger.info(
        "监听 %d 个路径: %s",
        len(paths),
        "、".join(str(path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path) for path in paths),
    )
    logger.info("规则: .py / prompts 下的模板 / 仓库根的 .env*（保存即重启，会掐断在飞回答）")

    stop = threading.Event()
    _install_stop_handlers(stop)

    restarts = 0
    child = subprocess.Popen(command, cwd=str(PROJECT_ROOT))
    try:
        for changes in watch(
            *paths,
            watch_filter=_select_change,
            debounce=debounce_ms,
            stop_event=stop,
            # 定期让出一次，让 `stop` 能被检查到；否则 Ctrl-C 要等到下一次文件变化才生效
            yield_on_timeout=True,
            raise_interrupt=False,
            ignore_permission_denied=True,
        ):
            if stop.is_set():
                break
            if not changes:
                continue

            changed = ", ".join(sorted({Path(path).name for _change, path in changes}))
            logger.warning("检测到变更（%s），重启后端…", changed)

            if child.poll() is not None:
                logger.warning("上一个子进程已自行退出（退出码 %s），按新配置重新启动", child.returncode)

            stop_child(child, graceful_timeout)
            child = subprocess.Popen(command, cwd=str(PROJECT_ROOT))
            restarts += 1
    finally:
        stop_child(child, graceful_timeout)

    logger.info("热重启已停止（本次共重启 %d 次）", restarts)
    return restarts


def _pid() -> int:
    """当前进程号。抽出来是为了让测试能替换（不要在模块里 `import os` 只为这一处）。"""
    import os

    return os.getpid()

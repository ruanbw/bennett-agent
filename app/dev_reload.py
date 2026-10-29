"""开发期热重启：改文件就往子进程发 SIGTERM 再起一个新的 `python main.py`。

`uvicorn --reload` 用 spawn 只 import `app.server:app`，会丢掉 `main.py` 里的日志格式与 no_proxy 清理，
所以启动路径必须与部署一致；模板与 `.env` 不进 import 缓存，只能靠重启；一次保存会掐断在飞的 SSE。
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

#: 会被 `PromptManager` 当成模板的后缀；与 `app/prompts/manager.py` 的 `TEMPLATE_SUFFIXES` 同源，改那边要跟着改。
TEMPLATE_SUFFIXES: tuple[str, ...] = (".jinja", ".jinja2", ".j2", ".txt")

#: 文件变更多久之后才重启（毫秒）：编辑器原子保存是临时文件 + rename，不防抖会连着重启两次。
DEFAULT_DEBOUNCE_MS = 500

#: 等子进程优雅退出的上限（秒），必须大于 uvicorn 的 `timeout_graceful_shutdown=5`，否则会 SIGKILL 正在收尾的进程。
DEFAULT_GRACEFUL_TIMEOUT = 6.0


def should_reload(path: Path) -> bool:
    """这个文件的变化是否要触发重启（纯函数）。

    必须排除 `*.db` 及 `-wal/-shm` 这类随请求变化的文件：当成变更源就变成「一有请求就重启」。
    """
    if path.name.startswith(".env"):
        # 只认 `get_env_files()` 列出的文件：glob 会把 `.env.example` 算进来，改它不该重启。
        return path.resolve() in _loaded_env_files()

    suffix = path.suffix.lower()
    if suffix == ".py":
        return True
    if suffix in TEMPLATE_SUFFIXES:
        # `.txt` 只在 prompts/ 下算模板：别把任意文档当成提示词变更
        return path.is_relative_to(PROJECT_ROOT / "prompts")
    return False


def _loaded_env_files() -> set[Path]:
    """settings 实际会加载的环境文件（`app/config/settings.py` 的 `get_env_files`）。

    必须复用它：叠加规则两处各写一份迟早漂移，表现是「改了 `.env.development.local` 却不重启」。
    """
    from app.config.settings import get_env_files

    return {path.resolve() for path in get_env_files()}


def watch_paths(extra: Sequence[str] = ()) -> list[Path]:
    """本次要监听的全部路径（只返回真实存在的）。

    `extra` 不过滤（调用方明确要求），但存在性检查不能省：`watch()` 拿到不存在的路径会抛异常。
    """
    candidates: list[Path] = [PROJECT_ROOT / directory for directory in WATCH_DIRS]
    candidates += [PROJECT_ROOT / name for name in WATCH_FILES]
    candidates += sorted(_loaded_env_files())
    candidates += [Path(item).expanduser().resolve() for item in extra]
    return [path for path in candidates if path.exists()]


def build_child_command(host: str, port: int) -> list[str]:
    """子进程命令：完整的 `main.py`，不带 `--reload`（否则会再起一个 supervisor 自递归）。

    用 `sys.executable` 而非裸 `python`：`uv run` 下它指向当前 venv，不受 shell 的 PATH 影响。
    """
    return [
        sys.executable,
        str(PROJECT_ROOT / "main.py"),
        "--host",
        host,
        "--port",
        str(port),
    ]


def stop_child(process: subprocess.Popen[bytes], graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT) -> None:
    """先 SIGTERM 等它自己退，超时才 SIGKILL。

    已退出的进程直接返回：启动失败时它早就死了，再 `terminate()` 会对已回收的 pid 发信号。
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

    不接管的话 Ctrl-C 会在 `watch()` 里抛 `KeyboardInterrupt`，跳过 finally 的清理，留下占端口的孤儿进程。
    """

    def _handle(signum: int, _frame: Any) -> None:  # noqa: ANN401 - signal 回调签名
        logger.info("收到信号 %s，准备退出…", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except ValueError:
            # 非主线程装不了信号处理器，退化成 Ctrl-C 抛 KeyboardInterrupt，由 try/finally 兜住清理。
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

    子进程自己崩了就停在那里等下一次保存，不立刻重拉：必崩的启动错误会刷满日志并吃满 CPU。
    """
    paths = watch_paths(extra_dirs)
    command = build_child_command(host, port)

    # 用日志而不是 print：格式与子进程一致，混在同一终端里时两行一眼能分开。
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

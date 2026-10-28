"""开发热重启（`app/dev_reload.py` 与 `main.py --reload`）。

这里锁的是**这个功能的边界**，而不是「watchfiles 能不能用」：

1. 哪些文件的变化该触发重启——最要紧的是**数据库/缓存副产物不能算**。业务库已经搬到
   Postgres，但脚本与临时调试仍可能落下 `*.db` / `-wal` / `-shm`，本地网关也有写入型
   缓存。它们随请求持续变化，当成变更源就等于「一有请求就重启」，而症状是「服务自己
   不停地重启」，很难联想到监听列表上；
2. 子进程必须走完整的 `main.py --web`，且**不带** `--reload`（否则无限自递归）；
3. production 下要直接拒绝，而不是警告后照跑。

这些都是纯逻辑，不需要真的起服务，因此全部按纯函数 / 参数检查来测。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import main as main_module
from app.dev_reload import (
    DEFAULT_DEBOUNCE_MS,
    PROJECT_ROOT,
    WATCH_DIRS,
    WATCH_FILES,
    _loaded_env_files,
    build_child_command,
    should_reload,
    watch_paths,
)


# ---------------------------------------------------------------------
# 监听范围
# ---------------------------------------------------------------------
def test_python_files_trigger_reload():
    assert should_reload(PROJECT_ROOT / "app" / "server.py") is True
    assert should_reload(PROJECT_ROOT / "main.py") is True


def test_prompt_templates_trigger_reload():
    """模板改动在装配缓存里看不到（Agent 的 system_prompt 是装配时固化的），必须重启。"""
    assert should_reload(PROJECT_ROOT / "prompts" / "system_prompt.jinja") is True


def test_env_files_at_project_root_trigger_reload():
    """settings 是模块级单例，只在 import 时读一次 .env。

    **不断言「至少加载了一个」**：`.env*` 被 `.gitignore` 忽略，干净的检出（以及 CI）
    本来就没有任何 .env 文件，而那时这个功能完全正常——没有文件要监听而已。曾经这里
    写的是 `assert loaded`，于是整个后端测试套件在每次全新 clone 上都是红的，而失败
    信息（「当前环境应至少加载一个 .env 文件」）指向的是测试环境而不是代码缺陷。
    """
    loaded = _loaded_env_files()
    if (PROJECT_ROOT / ".env.development") in loaded:
        assert should_reload(PROJECT_ROOT / ".env.development") is True
    # 清单里任何一个**存在**的文件都必须被监听（空清单是合法情形，见 docstring）
    for path in loaded:
        assert should_reload(path) is True, f"{path} 会被 settings 加载，却没被监听"


def test_env_example_does_not_trigger_reload():
    """`.env.example` 只是模板，settings 从不加载它——改它重启也不会有任何效果。"""
    assert (PROJECT_ROOT / ".env.example").is_file()
    assert should_reload(PROJECT_ROOT / ".env.example") is False


def test_env_file_not_loaded_for_current_env_does_not_trigger_reload():
    """非当前 APP_ENV 的环境文件不参与加载，改它不该重启。"""
    loaded = _loaded_env_files()
    for candidate in PROJECT_ROOT.glob(".env*"):
        if candidate.is_file() and candidate.resolve() not in loaded:
            assert should_reload(candidate) is False


def test_frontend_env_does_not_trigger_reload():
    """`frontend/.env` 是给 Vite 的（VITE_API_BASE_URL），与后端进程无关。"""
    assert should_reload(PROJECT_ROOT / "frontend" / ".env") is False


@pytest.mark.parametrize(
    "relative",
    [
        "bennett.db",
        "bennett.db-wal",
        "bennett.db-shm",
        "rate-limit.db",
        "README.md",
        "frontend/dist/index.html",
        "app/__pycache__/server.cpython-310.pyc",
    ],
)
def test_database_and_unrelated_files_do_not_trigger_reload(relative):
    """数据库/缓存副产物是硬要求：它们随请求持续变化，被监听会变成重启风暴。

    业务库已经不在这里（见 app/db.py），这些文件名保留为**防御性**用例：
    脚本或临时调试落下的 `*.db` 同样不该触发重启。
    """
    assert should_reload(PROJECT_ROOT / relative) is False


def test_txt_outside_prompts_is_not_a_template():
    """`.txt` 只在 prompts/ 下算模板，别把任意文本文件当成提示词变更。"""
    assert should_reload(PROJECT_ROOT / "docs" / "notes.txt") is False
    assert should_reload(PROJECT_ROOT / "prompts" / "extra.txt") is True


def test_watch_paths_exist_and_cover_the_declared_scope():
    paths = watch_paths()

    assert paths, "至少要监听 app/ 与 main.py"
    for path in paths:
        assert path.exists(), f"{path} 不存在时 watchfiles 会直接抛异常"


def test_watch_paths_include_every_env_file_present():
    """`.env` 的叠加规则由 settings 决定，写死清单必然漏；这里与 get_env_files 对齐。"""
    paths = set(watch_paths())
    for candidate in _loaded_env_files():
        if candidate.exists():
            assert candidate in paths


def test_watch_paths_exclude_env_example():
    """`.env.example` 不该出现在监听列表里：改它不会改变任何运行时行为。"""
    paths = {path.resolve() for path in watch_paths()}

    assert (PROJECT_ROOT / ".env.example").resolve() not in paths


def test_watch_paths_accepts_extra_dirs(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()

    assert extra.resolve() in watch_paths([str(extra)])


def test_watch_paths_skips_missing_extra_dirs(tmp_path):
    """调用方给了不存在的路径不能炸：watchfiles 拿到不存在的路径会抛异常。"""
    assert tmp_path / "nope" not in watch_paths([str(tmp_path / "nope")])


def test_declared_scope_matches_import_paths():
    """`app/` 与 `prompts/` 是进程真正加载的两块，`main.py` 是入口。"""
    assert "app" in WATCH_DIRS
    assert "prompts" in WATCH_DIRS
    assert "main.py" in WATCH_FILES


# ---------------------------------------------------------------------
# 子进程命令
# ---------------------------------------------------------------------
def test_child_command_uses_full_main_entrypoint():
    """子进程必须走 main.py，而不是 uvicorn —— 后者会丢掉 main() 里的启动动作。

    默认不带模式参数：`main.py` 的默认行为就是「全套」（API + 上传解析 + 沙箱回收）。
    热重启若丢了后台循环，症状是「改完代码后上传不再被解析」，比启动失败更难查。
    """
    command = build_child_command("127.0.0.1", 8000)

    assert command[0] == sys.executable
    assert any(part.endswith("main.py") for part in command)
    assert "--web" not in command and "--worker" not in command
    assert "--host" in command and "127.0.0.1" in command
    assert "--port" in command and "8000" in command


def test_child_command_can_be_web_only():
    """生产型的 `--web` 拓扑：热重启子进程也必须只起 API（不能顺带跑 worker 循环）。"""
    command = build_child_command("127.0.0.1", 8000, run_all=False)

    assert "--web" in command


def test_child_command_never_passes_reload():
    """子进程再开 reload 会无限自递归（supervisor 套 supervisor）。"""
    command = build_child_command("0.0.0.0", 9000)

    assert "--reload" not in command


# ---------------------------------------------------------------------
# main.py 的接线
# ---------------------------------------------------------------------
@pytest.fixture
def stub_uvicorn_run(monkeypatch):
    """拦下 uvicorn.run；返回调用记录。"""
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append({"args": args, "kwargs": kwargs}))
    return calls


def test_reload_flag_is_parsed():
    parser = main_module.build_arg_parser()

    assert parser.parse_args(["--reload"]).reload is True
    assert parser.parse_args(["--web"]).reload is False


def test_reload_delegates_to_supervisor_without_starting_uvicorn(monkeypatch, stub_uvicorn_run):
    monkeypatch.setattr(sys, "argv", ["main.py", "--reload", "--port", "8123"])

    recorded: list[dict] = []
    monkeypatch.setattr(
        main_module,
        "_run_dev_reload",
        lambda host, port, **kwargs: recorded.append({"host": host, "port": port, **kwargs}) or 0,
    )

    main_module.main()

    # 不带模式参数 = 全套：本地开发正是这个意图。
    assert recorded == [{"host": "127.0.0.1", "port": 8123, "run_all": True}]
    assert stub_uvicorn_run == [], "热重启模式下不应直接起 uvicorn（会丢掉启动动作）"


def test_reload_keeps_web_topology_when_web_is_explicit(monkeypatch, stub_uvicorn_run):
    """`--reload --web` 是生产型拓扑：子进程也只能起 API。"""
    monkeypatch.setattr(sys, "argv", ["main.py", "--reload", "--web", "--port", "8123"])

    recorded: list[dict] = []
    monkeypatch.setattr(
        main_module,
        "_run_dev_reload",
        lambda host, port, **kwargs: recorded.append({"host": host, "port": port, **kwargs}) or 0,
    )

    main_module.main()

    assert recorded == [{"host": "127.0.0.1", "port": 8123, "run_all": False}]


def test_reload_is_rejected_in_production(monkeypatch, stub_uvicorn_run):
    """生产环境直接拒绝：那里任何一次重启都会掐断在飞请求，且原因不该是「文件变了」。"""
    from app.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(sys, "argv", ["main.py", "--reload"])

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2  # argparse 的用法错误退出码
    assert stub_uvicorn_run == []


def test_default_debounce_is_positive():
    """防抖为 0 时编辑器的原子保存（临时文件 + rename）会连着重启两次。"""
    assert DEFAULT_DEBOUNCE_MS > 0


def test_project_root_is_the_repo_root():
    assert (PROJECT_ROOT / "main.py").is_file()
    assert (PROJECT_ROOT / "app").is_dir()


def test_stop_child_is_a_noop_for_finished_process(tmp_path):
    """已退出的子进程不能再 terminate：那会对已回收的 pid 发信号。"""
    import subprocess

    from app.dev_reload import stop_child

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()

    stop_child(process)  # 不该抛异常

    assert process.poll() is not None


def test_stop_child_kills_a_process_that_ignores_sigterm(tmp_path):
    """SIGTERM 超时后必须升级为 SIGKILL，否则一次保存会卡死在这里。"""
    import subprocess

    from app.dev_reload import stop_child

    process = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
    )
    try:
        stop_child(process, graceful_timeout=0.5)
        assert process.poll() is not None
    finally:
        if process.poll() is None:  # 兜底，别在测试里留孤儿进程
            process.kill()
            process.wait()


def test_requirements_are_pathlike_not_strings():
    """监听路径必须是 Path：`should_reload` 用 `.parent` / `.is_relative_to` 判断归属。"""
    for path in watch_paths():
        assert isinstance(path, Path)

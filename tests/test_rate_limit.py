"""进程内限流的算法与启动参数校验（`app/api/rate_limit.py`、`main.py`）。

HTTP 层用例（429 响应、按账号/按 IP 分流、端点接线）已随全量/端到端层移除；这里保留
`SlidingWindowLimiter` 的算法行为、来源识别（`resolve_client_ip` / `client_key`），
以及 `main.py` 的启动参数校验。
"""

import sys
import threading

import pytest

from app.api.rate_limit import SlidingWindowLimiter, client_key, resolve_client_ip


def test_allows_up_to_limit():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)

    assert [limiter.check("ip")[0] for _ in range(3)] == [True, True, True]


def test_blocks_beyond_limit_and_suggests_retry_after():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("ip")
    limiter.check("ip")

    allowed, retry_after = limiter.check("ip")

    assert allowed is False
    assert 0 < retry_after <= 60


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("a")

    assert limiter.check("b")[0] is True
    assert limiter.check("a")[0] is False


def test_window_slides(monkeypatch):
    """窗口之外的命中要被丢掉，否则限流器会永久锁死一个来源。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.api.rate_limit.time.monotonic", lambda: clock["now"])

    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert limiter.check("ip")[0] is True
    assert limiter.check("ip")[0] is False

    clock["now"] += 11
    assert limiter.check("ip")[0] is True


def test_tracked_keys_are_capped():
    """伪造来源不能把内存吃满。"""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=5)

    for index in range(20):
        limiter.check(f"ip-{index}")

    assert len(limiter._hits) <= 5


def test_tracked_keys_evict_expired_and_lru():
    """超限时不应清空全表，而是清理过期项或淘汰最旧项。"""
    limiter = SlidingWindowLimiter(limit=5, window_seconds=10, max_keys=3)

    limiter.check("ip-1")
    limiter.check("ip-2")
    limiter.check("ip-3")

    # 4th key 触发淘汰，由于未过期，最久未访问的 ip-1 应被淘汰，ip-2 和 ip-3 保留
    limiter.check("ip-4")
    assert len(limiter._hits) <= 3
    assert "ip-1" not in limiter._hits
    assert "ip-2" in limiter._hits
    assert "ip-3" in limiter._hits
    assert "ip-4" in limiter._hits


def test_eviction_prefers_expired_key_over_still_usable_key(monkeypatch):
    """容量满时先丢窗口内已无命中的键，而不是丢掉还能继续用的键。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.api.rate_limit.time.monotonic", lambda: clock["now"])

    limiter = SlidingWindowLimiter(limit=5, window_seconds=10, max_keys=2)
    limiter.check("stale")
    clock["now"] = 1016.0
    limiter.check("live")

    clock["now"] = 1020.0  # cutoff=1010：stale 已过期，live 仍在窗口内
    limiter.check("newcomer")

    assert "stale" not in limiter._hits
    assert "live" in limiter._hits


def test_lru_eviction_never_resets_a_banned_key():
    """max_keys 小时 churn 新 key 不能重置已封禁的键。

    已封禁的键命中停留在封禁那一刻，按「最近命中时间」淘汰它恰好最旧，于是依次插入
    几个新 key 就能消掉自己的封禁。
    """
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, max_keys=2)

    assert [limiter.check("victim")[0] for _ in range(3)] == [True, True, True]
    assert limiter.check("victim")[0] is False  # 已封禁
    assert limiter.check("filler")[0] is True

    # 表满：该淘汰未达限的 filler，而不是把封禁中的 victim 丢掉
    assert limiter.check("newcomer")[0] is True

    assert "victim" in limiter._hits
    assert limiter.check("victim")[0] is False


def test_zero_or_negative_limit_means_unlimited():
    """与 settings 的「0=关闭」一致：limit <= 0 直接放行，而不是全拒绝。"""
    limiter = SlidingWindowLimiter(limit=0, window_seconds=60)
    assert limiter.check("ip") == (True, 0.0)

    limiter_neg = SlidingWindowLimiter(limit=-5, window_seconds=30)
    assert limiter_neg.check("ip") == (True, 0.0)
    # 不限流时不该白白记录状态
    assert limiter_neg._hits == {}


@pytest.mark.parametrize("max_keys", [0, -1])
def test_max_keys_must_be_positive(max_keys):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=max_keys)


@pytest.mark.parametrize("window_seconds", [0, -1.0])
def test_window_seconds_must_be_positive(window_seconds):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=1, window_seconds=window_seconds)


def test_hit_exactly_on_window_boundary_is_expired(monkeypatch):
    """窗口是 `(now - window, now]`：恰好落在左端点的命中已经过期，不该继续占配额。"""
    clock = {"now": 500.0}
    monkeypatch.setattr("app.api.rate_limit.time.monotonic", lambda: clock["now"])

    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert limiter.check("ip")[0] is True

    clock["now"] = 509.999
    assert limiter.check("ip")[0] is False

    clock["now"] = 510.0
    assert limiter.check("ip")[0] is True


def test_concurrent_checks_are_safe():
    """并发冒烟：多线程混跑下不抛异常、追踪表上限依然成立。

    真实调用来自 asyncio 事件循环（check 是纯同步函数，单线程内天然原子），这里再加
    一层线程压力。
    """
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60, max_keys=3)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait()
            for index in range(200):
                limiter.check(f"ip-{worker_id % 3}-{index % 7}")
        except BaseException as exc:  # pragma: no cover - 只有并发 bug 才走到
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(limiter._hits) <= 3


def test_resolve_client_ip():
    """验证反向代理 IP 识别逻辑。"""
    # 无受信任标头时返回直连 IP
    assert resolve_client_ip("127.0.0.1", headers={"x-forwarded-for": "10.0.0.1"}) == "127.0.0.1"

    # 有受信任标头时取**最右**值：那是最后一跳代理追加的真实来源
    trusted = ["X-Forwarded-For", "X-Real-IP"]
    headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18, 150.172.238.178"}
    assert resolve_client_ip("127.0.0.1", headers=headers, trusted_headers=trusted) == "150.172.238.178"

    headers_real_ip = {"X-Real-IP": "198.51.100.1"}
    assert resolve_client_ip("127.0.0.1", headers=headers_real_ip, trusted_headers=trusted) == "198.51.100.1"

    # headers 为空或标头值全为空时回退直连 IP
    assert resolve_client_ip("127.0.0.1", headers={}, trusted_headers=trusted) == "127.0.0.1"
    assert resolve_client_ip("127.0.0.1", headers={"X-Forwarded-For": "  "}, trusted_headers=trusted) == "127.0.0.1"
    assert resolve_client_ip(None, headers={}, trusted_headers=trusted) == "unknown"


def test_client_forged_xff_leftmost_is_ignored():
    """客户端可以把最左值写成任意内容；只有代理追加的最右值能决定限流键。"""
    trusted = ["X-Forwarded-For"]
    forged = {"X-Forwarded-For": "6.6.6.6, 7.7.7.7, 203.0.113.9"}

    assert resolve_client_ip("127.0.0.1", headers=forged, trusted_headers=trusted) == "203.0.113.9"

    # 代理异常或手工拼接会在末尾留下空段：取最右**非空**值
    trailing = {"X-Forwarded-For": "203.0.113.9, , "}
    assert resolve_client_ip("127.0.0.1", headers=trailing, trusted_headers=trusted) == "203.0.113.9"
    assert resolve_client_ip("127.0.0.1", headers={"X-Forwarded-For": " , "}, trusted_headers=trusted) == "127.0.0.1"


def test_reset_clears_state():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("ip")

    limiter.reset()

    assert limiter.check("ip")[0] is True


def test_client_key_falls_back_for_missing_host():
    """缺来源时归入同一个桶，宁可保守也不要放过。"""
    assert client_key(None) == "unknown"
    assert client_key("1.2.3.4") == "1.2.3.4"


def test_client_key_scope_splits_buckets_per_endpoint():
    """同一 IP 的 login / register 是两个独立桶。

    共用一个月会造出一个很难解释的失败模式：有人在一个 NAT 出口后面猛试密码，把
    10 次/分钟打满之后，同一出口的**正常注册**也做不了（反之亦然）。
    """
    assert client_key("1.2.3.4", "login") != client_key("1.2.3.4", "register")
    assert client_key("1.2.3.4", "login") == "login:1.2.3.4"
    # 无 scope 时保持旧行为（对话端点等调用点不受影响）
    assert client_key("1.2.3.4") == "1.2.3.4"


# ---------------------------------------------------------------------
# 启动参数（main.py）
# ---------------------------------------------------------------------
def stub_uvicorn_run(monkeypatch) -> list[dict]:
    """拦下 uvicorn.run，避免用例真的起服务；返回调用记录。"""
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append({"args": args, "kwargs": kwargs}))
    return calls


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_out_of_range_port_is_rejected(monkeypatch, port):
    import main as main_module

    monkeypatch.setattr(sys, "argv", ["main.py", "--web", "--port", port])
    calls = stub_uvicorn_run(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2  # argparse 的用法错误退出码
    assert calls == []  # 校验发生在启动之前

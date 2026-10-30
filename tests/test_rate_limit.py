"""限流的算法、降级语义与启动参数校验（`app/api/rate_limit.py`、`main.py`）。

HTTP 层用例（429 响应、按账号/按 IP 分流、端点接线）已随全量/端到端层移除；这里保留：

- `SlidingWindowLimiter` 的进程内算法（Redis 不可用时的降级实现）；
- `RedisSlidingWindowLimiter` 的共享计数、窗口过期与三种失败模式；
- 来源识别（`resolve_client_ip` / `client_key`）。
"""

import asyncio
import sys
import threading

import pytest
from fastapi import HTTPException

from app.api.rate_limit import SlidingWindowLimiter, bucket_ip_address, client_key, resolve_client_ip
from app.config import settings


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
    """验证反向代理 IP 识别逻辑：直连对端必须是受信代理，且从左往右跳过受信地址。"""
    proxies = "10.0.0.0/8,127.0.0.1/32"
    trusted = ["X-Forwarded-For", "X-Real-IP"]

    # 无受信任标头时返回直连 IP
    assert resolve_client_ip("127.0.0.1", headers={"x-forwarded-for": "10.0.0.1"}) == "127.0.0.1"

    # 有标头但没配代理网段：不采信，退回直连（fail closed）
    assert resolve_client_ip("198.51.100.7", headers={"X-Forwarded-For": "203.0.113.9"}, trusted_headers=trusted) == (
        "198.51.100.7"
    )

    # 直连对端是受信代理：从右往左取第一个**非代理**地址
    headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18, 150.172.238.178"}
    assert (
        resolve_client_ip("10.0.0.5", headers=headers, trusted_headers=trusted, trusted_proxies=proxies)
        == "150.172.238.178"
    )

    # 两跳代理：最右是受信 CDN，继续往左才是真实客户端
    two_hop = {"X-Forwarded-For": "203.0.113.9, 10.1.2.3"}
    assert (
        resolve_client_ip("10.0.0.5", headers=two_hop, trusted_headers=["X-Forwarded-For"], trusted_proxies=proxies)
        == "203.0.113.9"
    )

    headers_real_ip = {"X-Real-IP": "198.51.100.1"}
    assert (
        resolve_client_ip("10.0.0.5", headers=headers_real_ip, trusted_headers=trusted, trusted_proxies=proxies)
        == "198.51.100.1"
    )

    # headers 为空或标头值全为空时回退直连 IP
    assert resolve_client_ip("10.0.0.5", headers={}, trusted_headers=trusted, trusted_proxies=proxies) == "10.0.0.5"
    assert (
        resolve_client_ip(
            "10.0.0.5", headers={"X-Forwarded-For": "  "}, trusted_headers=trusted, trusted_proxies=proxies
        )
        == "10.0.0.5"
    )
    assert resolve_client_ip(None, headers={}, trusted_headers=trusted, trusted_proxies=proxies) == "unknown"


def test_resolve_client_ip_ignores_unparseable_entries():
    """无法解析成 IP 的条目不能变成可自选的限流键。"""
    result = resolve_client_ip(
        "10.0.0.5",
        headers={"X-Forwarded-For": "6.6.6.6, not-an-ip, 203.0.113.9"},
        trusted_headers=["X-Forwarded-For"],
        trusted_proxies="10.0.0.0/8",
    )
    assert result == "203.0.113.9"


def test_resolve_client_ip_handles_bracketed_ipv6():
    result = resolve_client_ip(
        "10.0.0.5",
        headers={"X-Forwarded-For": "[2001:db8::1]:443"},
        trusted_headers=["X-Forwarded-For"],
        trusted_proxies="10.0.0.0/8",
    )
    assert result == "2001:db8::/64"


def test_direct_client_cannot_spoof_proxy_headers():
    """L-10：直连对端不在受信网段时，X-Forwarded-For 完全不被采信。"""
    assert (
        resolve_client_ip(
            "198.51.100.7",
            headers={"X-Forwarded-For": "6.6.6.6"},
            trusted_headers=["X-Forwarded-For"],
            trusted_proxies="10.0.0.0/8",
        )
        == "198.51.100.7"
    )


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

    monkeypatch.setattr(sys, "argv", ["main.py", "--port", port])
    calls = stub_uvicorn_run(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2  # argparse 的用法错误退出码
    assert calls == []  # 校验发生在启动之前


def test_ipv6_rotating_addresses_share_a_bucket():
    """同一 /64 内换地址不能绕过限流（I-07）。"""
    first = bucket_ip_address("2001:db8:1:2::1")
    second = bucket_ip_address("2001:db8:1:2:ffff::9")

    assert first == second == "2001:db8:1:2::/64"
    assert bucket_ip_address("10.0.0.1") == "10.0.0.1"
    assert bucket_ip_address("::ffff:10.0.0.1") == "10.0.0.1"


# ---------------------------------------------------------------------
# 跨进程共享限流（I-07）：Redis ZSET + Lua
# ---------------------------------------------------------------------
async def test_redis_limiter_is_shared_between_instances():
    """两个 worker 各自 new 一个 limiter，但配额必须来自同一份共享计数。"""
    from app.api.rate_limit import RedisSlidingWindowLimiter

    worker_a = RedisSlidingWindowLimiter(limit=2, window_seconds=60)
    worker_b = RedisSlidingWindowLimiter(limit=2, window_seconds=60)
    await worker_a.reset()

    assert (await worker_a.acheck("shared-ip"))[0] is True
    assert (await worker_b.acheck("shared-ip"))[0] is True
    allowed, retry_after = await worker_a.acheck("shared-ip")

    assert allowed is False
    assert 0 < retry_after <= 60


async def test_redis_limiter_window_expires():
    """窗口过后计数归零：键的 PEXPIRE 与窗口一致，不需要额外清理任务。"""
    from app.api.rate_limit import RedisSlidingWindowLimiter

    limiter = RedisSlidingWindowLimiter(limit=1, window_seconds=1)
    await limiter.reset()

    assert (await limiter.acheck("expiring-ip"))[0] is True
    assert (await limiter.acheck("expiring-ip"))[0] is False

    await asyncio.sleep(1.1)
    assert (await limiter.acheck("expiring-ip"))[0] is True
    await limiter.reset()


async def test_redis_limiter_falls_back_to_local_counting(monkeypatch):
    """Redis 抖动时按 `local_fallback` 降级：**仍有配额约束**，且不会抛给调用方。"""
    import app.services.redis_client as redis_client
    from app.api.rate_limit import RedisSlidingWindowLimiter

    class BrokenRedis:
        async def eval(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())
    monkeypatch.setattr(settings, "rate_limit_on_redis_error", "local_fallback")

    limiter = RedisSlidingWindowLimiter(limit=1, window_seconds=60)
    assert (await limiter.acheck("degraded-ip"))[0] is True
    assert (await limiter.acheck("degraded-ip"))[0] is False


async def test_redis_limiter_fail_closed_mode(monkeypatch):
    """`fail_closed`：宁可拒绝也不放宽——适合把配额当安全边界的部署。"""
    import app.services.redis_client as redis_client
    from app.api.rate_limit import RedisSlidingWindowLimiter

    class BrokenRedis:
        async def eval(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())
    monkeypatch.setattr(settings, "rate_limit_on_redis_error", "fail_closed")

    limiter = RedisSlidingWindowLimiter(limit=5, window_seconds=60)
    allowed, retry_after = await limiter.acheck("closed-ip")

    assert allowed is False
    assert retry_after == 60


# ---------------------------------------------------------------------
# 在途流上限（并发维度）
#
# 滑动窗口只能回答「每分钟发了多少次」，而一条 SSE 最长挂 600s：单账号每分钟 30 次全部
# 合法，却可以让 30 条流同时占着上游配额与连接池，失败的是别人的请求。并发必须单独限。
# 这里锁住三件事：超限拒绝（429 + Retry-After）、正常结束后名额归还、Redis 挂了仍有限制。
# ---------------------------------------------------------------------
async def test_inflight_limiter_blocks_beyond_the_limit_and_frees_on_release():
    import uuid

    from app.api.rate_limit import InFlightStreamLimiter

    limiter = InFlightStreamLimiter(limit=2)
    key = f"user:{uuid.uuid4().hex}"

    assert await limiter.acquire(key) is True
    assert await limiter.acquire(key) is True
    assert await limiter.acquire(key) is False

    await limiter.release(key)
    assert await limiter.acquire(key) is True


async def test_inflight_stream_slot_returns_429_for_the_next_stream():
    """超限的第 N+1 条流拿 429（带 Retry-After），正常结束的流要还回名额。"""
    import uuid
    from contextlib import AsyncExitStack

    from app.api.rate_limit import (
        CHAT_MAX_INFLIGHT_STREAMS,
        INFLIGHT_RETRY_AFTER_SECONDS,
        inflight_stream_slot,
    )

    # 唯一账号：用例之间不互相污染，也不必依赖 reset()
    user_id = uuid.uuid4().hex

    held = AsyncExitStack()
    for _ in range(CHAT_MAX_INFLIGHT_STREAMS):
        await held.enter_async_context(inflight_stream_slot(user_id))

    with pytest.raises(HTTPException) as excinfo:
        async with inflight_stream_slot(user_id):
            pass

    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == str(INFLIGHT_RETRY_AFTER_SECONDS)
    assert "同时进行的对话太多" in excinfo.value.detail

    await held.aclose()
    # 名额已归还：同一账号可以再开一条
    async with inflight_stream_slot(user_id):
        pass


async def test_inflight_stream_slot_is_per_account():
    """一个账号占满自己的名额，不能把别人的对话也堵死。"""
    import uuid
    from contextlib import AsyncExitStack

    from app.api.rate_limit import CHAT_MAX_INFLIGHT_STREAMS, inflight_stream_slot

    hog = uuid.uuid4().hex
    other = uuid.uuid4().hex

    async with AsyncExitStack() as held:
        for _ in range(CHAT_MAX_INFLIGHT_STREAMS):
            await held.enter_async_context(inflight_stream_slot(hog))

        async with inflight_stream_slot(other):
            pass


async def test_inflight_stream_slot_releases_on_error():
    """流里抛异常也必须归还名额，否则一次失败把账号锁到 TTL 过期。"""
    import uuid

    from app.api.rate_limit import (
        CHAT_MAX_INFLIGHT_STREAMS,
        get_inflight_limiter,
        inflight_stream_slot,
    )

    user_id = uuid.uuid4().hex
    limiter = get_inflight_limiter()

    with pytest.raises(RuntimeError):
        async with inflight_stream_slot(user_id):
            raise RuntimeError("流里炸了")

    # 归还后还能再占满整个配额，说明上面那条确实还回去了
    for _ in range(CHAT_MAX_INFLIGHT_STREAMS):
        assert await limiter.acquire(f"user:{user_id}") is True
    for _ in range(CHAT_MAX_INFLIGHT_STREAMS):
        await limiter.release(f"user:{user_id}")


async def test_inflight_limiter_degrades_to_local_counting(monkeypatch):
    """Redis 不可用时仍有上限约束（配额乘以实例数，但不会变成无限制）。"""
    import uuid

    import app.services.redis_client as redis_client
    from app.api.rate_limit import InFlightStreamLimiter

    class BrokenRedis:
        async def incr(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())

    limiter = InFlightStreamLimiter(limit=1)
    key = f"user:{uuid.uuid4().hex}"

    assert await limiter.acquire(key) is True
    assert await limiter.acquire(key) is False

    await limiter.release(key)
    assert await limiter.acquire(key) is True


async def test_inflight_limiter_releases_through_the_same_backend(monkeypatch):
    """降级路径的归还不走 Redis：降级期间占的名额必须能减回去。"""
    import uuid

    import app.services.redis_client as redis_client
    from app.api.rate_limit import InFlightStreamLimiter

    class BrokenRedis:
        async def incr(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: BrokenRedis())

    limiter = InFlightStreamLimiter(limit=2)
    key = f"user:{uuid.uuid4().hex}"
    assert await limiter.acquire(key) is True

    await limiter.release(key)

    assert limiter._local == {}


@pytest.mark.parametrize("limit", [0, -1])
def test_inflight_limit_must_be_positive(limit):
    from app.api.rate_limit import InFlightStreamLimiter

    with pytest.raises(ValueError):
        InFlightStreamLimiter(limit=limit)

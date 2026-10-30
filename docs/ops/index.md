---
title: 运维总览
description: 这个服务在后台跑着哪几条回路，出了症状先看哪里。
---

# 运维总览

Bennett Agent 是一个长驻进程：一条 HTTP 服务 + 两条常驻后台回路。真正需要盯的只有三处——健康与指标、沙箱回收、同步长轮询。本页给出这三条回路的边界，以及「症状 → 先看哪里」的对照表；动手改代码前先看 [测试体系](testing.md)，线上出问题时查 [可观测性](observability.md) 与 [故障排查](troubleshooting.md)。

## 三条运行回路

### 1. 请求回路：健康、指标与关联日志

每个 HTTP 请求都会经过两层纯 ASGI 中间件：`RequestContextMiddleware` 分配请求 ID 并写进响应头与日志行，`HttpMetricsMiddleware` 记请求数与耗时。探针与指标端点挂在同一进程上：

| 端点 | 作用 | 依赖 |
|---|---|---|
| `GET /health` | 存活探针，只回 `status` 与版本号 | 不碰数据库与上游 |
| `GET /health/ready` | 就绪探针，逐项探测 | database / redis / storage / provider_keys |
| `GET /api/metrics` | 进程内指标，Prometheus 文本格式 | 默认关闭（`METRICS_ENABLED=false`） |

`/health` 故意不去 ping 数据库：数据库抖动时让 liveness 失败等于把「重启解决不了的问题」变成一次重启风暴。就绪判定看 `/health/ready`——只有 `READINESS_REQUIRED_DEPENDENCIES`（默认 `database`）里的依赖失败才回 503，其余降级成 `degraded` 并仍然返回 200。

### 2. 沙箱回收回路

`main.py` 的 `_run_all` 在**同一个事件循环**里起 `run_reaper(stop)`，每 `REAP_INTERVAL_SECONDS = 60` 秒跑一轮 `reap_once()`。一轮里的判定顺序固定：心跳 → 条件 UPDATE 抢占 `destroying` → 导出产物 → 销毁机器。

这条回路必须常驻。触发条件是「没人发请求了」，挂在请求路径上就永远不会触发。销毁失败不是终态：租约落成 `destroy_failed`，下一轮回收会重新抢它；只有机器真的被 provider 确认销毁才写 `destroyed`。

### 3. 同步长轮询回路

`GET /api/sync/pull` 在没有新数据时挂起，最多 `SYNC_LONG_POLL_SECONDS`（默认 25 秒）。唤醒靠 `InMemoryNotifier`——每用户一个进程内 `asyncio.Event`。

这是**单进程实现**。`uvicorn --workers 2` 以上时，A 进程的写入唤不醒 B 进程的等待方：不报错、不告警，只会静默等到长轮询超时。挂起的连接还各占一个协程与一条 HTTP 连接，所以默认挂起时间给得短。

## 症状 → 先看哪里

| 症状 | 先看哪里 | 命令或日志关键字 |
|---|---|---|
| 服务起不来，进程直接退出 | 启动日志的第一条 ERROR | `提示词自检失败` / `数据库结构已漂移` / `生产环境必须设置强随机 AUTH_SECRET` |
| 服务活着但一直 503 | `/health/ready` 的 `status` 与 `required` | `curl -s http://127.0.0.1:8000/health/ready` |
| 对话流式输出卡住不推进 | 指标里的 `chat_first_event_seconds` + 该请求的日志行 | 日志里的 `[request_id]` 字段，与响应头 `X-Request-ID` 同值 |
| 上游报 401，模型像是没配 key | 启动日志里的 API Key 解密自检 | `API Key 解密失败（AUTH_SECRET 可能已更换）` |
| 沙箱机器没被回收 | 回收循环每轮的那行汇总 | `沙箱回收一轮：候选=… 已销毁=… 失败=…` |
| 用户报「执行环境资源已满」 | 容量位（`sandbox_max_concurrent` / `sandbox_max_per_user`）与租约状态 | `回收：租约卡在 provisioning` / `回收：销毁失败` |
| 跨设备同步迟迟不更新 | 长轮询挂起与游标版本 | `code=resync_required` / `code=invalid_cursor` |
| 频繁 429 | 限流桶（对话按账号、认证按来源 IP、同步单独一个桶） | 响应头 `Retry-After` 与 `X-RateLimit-Remaining` |
| 指标端点 404 | `METRICS_ENABLED` | `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/metrics` |
| 前端空白页 / 请求 404 | Vite dev server 的 `/api` 代理与后端端口 | `cd frontend && npm run dev`，代理目标 `http://127.0.0.1:8000` |

## 边界与常见坑

- **回收回路不能停。** 停掉它 = 机器永久泄漏，每个用户一个容量位被永久占住。
- **长轮询只支持单进程。** 多 worker 部署要么改用共享 pub/sub，要么接受同步延迟一个挂起周期。
- **指标是进程内的。** 多 worker 时每个进程各一份 `/api/metrics`，不能当计费依据；它是排障辅助。
- **`/api/metrics` 免登录。** 它默认关闭就是为了少开一个侦察面；打开就要同时用反代把它挡在内网或白名单后面。
- **就绪探针不检查上游模型。** 模型挂了不该摘掉整个实例——那样连历史会话都看不了。

## 相关源码

- `main.py`：入口，装配 API 与沙箱回收循环，停机顺序在这里定
- `app/api/ops.py`：`/health`、`/health/ready`、`/api/metrics`
- `app/observability.py`：请求 ID 中间件与 HTTP 指标中间件
- `app/services/metrics.py`：指标注册表与指标名定义
- `app/services/sandbox/reaper.py`：回收循环
- `app/services/sync_notifier.py`：长轮询唤醒（进程内实现）
- `app/server.py`：启动自检与中间件注册顺序

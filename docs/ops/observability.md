---
title: 可观测性
description: 健康探针、Prometheus 指标名与标签、请求 ID 传递、日志格式与审计产物。
---

# 可观测性

可观测性分四层：探针（服务还能不能干活）、指标（趋势与分布）、日志（单条请求发生了什么）、审计（谁做了什么）。四层的关联键不同：日志行与 500 响应体靠请求 ID 串起来，指标按 `method` / `route` / `status` 聚合，审计行按 `actor` / `tenant` 独立成流。

## 探针

### `GET /health`

存活探针。**不碰数据库与上游**，只返回 `{"status": "ok", "version": "..."}`。

它故意不去 ping 数据库：数据库抖动时让 liveness 失败，等于把「重启解决不了的问题」变成一次重启风暴，还要丢掉在途请求。就绪判定看 `/health/ready`。

### `GET /health/ready`

就绪探针。逐项探测依赖，按配置决定是否把实例摘出负载均衡：

| 检查项 | 怎么判 | 失败时 |
|---|---|---|
| `database` | `SELECT 1`，超时 `READY_DB_TIMEOUT_SECONDS = 2.0` 秒 | `{"status": "unavailable", "reason": "timeout"｜"unavailable"}` |
| `redis` | `redis_health_detail(timeout=2.0)`，不泄露连接串 | `{"status": "unavailable", "reason": "timeout"｜异常类名}` |
| `storage` | 对象存储健康检查；未开沙箱时显式报 `disabled` | `{"status": "unavailable", "reason": "not_configured"｜"error"}` |
| `provider_keys` | 逐条试解提供商 API Key | `{"status": "degraded", "reason": "undecryptable", "providers": [...]}`；自检抛异常时是 `{"status": "unknown", "reason": "error"}` |

判定规则：

- `required` 里的依赖（`READINESS_REQUIRED_DEPENDENCIES`，默认 `database`）有任一失败 → **503**，整体 `not_ready`。
- 其余依赖失败 → 整体 `degraded`，**仍然返回 200**。`storage` 的 `disabled` 不算失败。
- 全部正常 → `ready`。

`provider_keys` 不参与 `required` 判定：它只影响对话，算成不健康会让「只有模型调用失败」的实例被整体摘流。`READINESS_REQUIRED_DEPENDENCIES` 认 `database`/`redis`/`storage`/`provider_keys` 四个名字，写错名字会被忽略并打一条 WARNING——写错一个词不该让启动失败，但也不能静默当成「不用检查」。

```bash
curl -s http://127.0.0.1:8000/health/ready | python -m json.tool
```

## 指标端点

`GET /api/metrics` 输出 Prometheus 文本格式（`text/plain; version=0.0.4; charset=utf-8`）。

**默认关闭**（`METRICS_ENABLED=false`），关闭时返回 **404 而不是 403**：免登录端点多开一个就多一分侦察面，404 比「存在但你不能看」泄露得更少。打开后建议同时用反向代理把它挡在内网或白名单后面——它没有认证。

指标是**进程内**的：多 worker 时每个进程各一份，不能当计费依据。采集时要按实例分别抓再汇总，单个实例的数字只代表它自己处理的那部分流量。

```bash
curl -s http://127.0.0.1:8000/api/metrics | head -20
```

### 指标清单

指标名常量集中在 `app/services/metrics.py`（唯一例外是 `redis_unavailable_total`，字面量写在 `app/services/redis_client.py` 的 `warn_throttled` 里）：

| 指标名 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `http_requests_total` | counter | `method`、`route`、`status` | HTTP 请求数。`route` 是路由模板不是实际路径；`status` 是分类（`2xx`/`4xx`/`5xx`/`none`），`none` 是响应前就断开。`/health*` 与 `/api/metrics` 本身不统计 |
| `http_request_duration_seconds` | histogram | `method`、`route` | 请求耗时。计时覆盖到响应发送完毕，`/api/chat` 是整条 SSE 流的时长 |
| `chat_streams_total` | counter | `outcome` | 对话流终态数。正常收尾是 `done`/`error`/`stopped`/`timeout`/`approval_required`；没进流式循环就被拒的轮次另记 `model_unavailable`/`context_overflow`/`invalid_history`，否则错误率永远算不出来 |
| `chat_first_event_seconds` | histogram | 无 | 首字节耗时——用户感知的「卡不卡」主要就是这一段 |
| `context_compactions_total` | counter | `result`（`compacted`/`rejected`） | 上下文压缩次数，是「会话还能用多久」的先行指标 |
| `tool_executions_total` | counter | `tool`、`outcome` | 工具执行。`outcome` 取 `success`/`failed`/`denied`/`interrupted`；失败率 = `failed` / (`success` + `failed`)，审批与中断不算故障 |
| `model_fallbacks_total` | counter | `from`、`to` | 备用模型切换次数 |
| `redis_unavailable_total` | counter | `reason` | Redis 降级次数。当前代码里只发 `rate_limit` / `search_cache` 两个值；会话热缓存失败只记 warning，不打这个指标 |

延迟直方图的桶固定在 `DEFAULT_BUCKETS`：0.005s 到 600s，跨度按「大致翻倍」而非等距——延迟是长尾分布。上界取 600 秒（对话流的总预算），取小了长流全落进 `+Inf`，首字延迟的 p95 从此不可读。

标签值必须来自有限集合。单条指标最多保留 `MAX_SERIES_PER_METRIC = 200` 个标签组合，超出后丢弃并计数——它防的是「某个标签值其实是用户输入」这类实现失误。因此 `route` 用路由模板，404 走固定的 `<unmatched>`。

## 请求关联 ID

`RequestContextMiddleware` 给每个请求分配一个 12 位十六进制的 ID，同时出现在三处：

- 响应头 `X-Request-ID`；
- 每条日志行的 `[request_id]` 字段；
- 未捕获异常的 500 响应体里的 `error_id`。

客户端可以自带 `X-Request-ID`（网关已生成时不必再造），但会被 `sanitize_request_id` 校验：长度不超过 64、字符集限 ASCII 字母数字加 `._-`。不合法的值被丢弃并自己生成——它会原样写进日志行与响应头，换行符能伪造一整条假日志。

日志里没有请求上下文时（启动日志、后台任务）填 `-`，一眼能看出这不是请求日志。

```bash
curl -i -s http://127.0.0.1:8000/health | grep -i x-request-id
```

## 结构化日志

日志格式在 `app/config/logging_setup.py` 里定死：

```text
%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s | %(message)s
```

要点：

- uvicorn 的三个 logger（`uvicorn`、`uvicorn.error`、`uvicorn.access`）被换成同一套格式，`propagate=False` 避免同一行打印两遍。
- 访问日志默认关闭（`LOG_ACCESS`）：SSE 与前端轮询会让它变成噪音。
- `httpx` / `httpcore` / `openai` / `urllib3` / `asyncio` 在 DEBUG 下也被压到 WARNING——它们会打印请求头，里面有 `Authorization` 与 API Key。
- 未处理异常统一返回 500 + 关联 ID，不回异常原文（可能含 SQL、路径与凭据）。日志关键字：`未处理异常 (error_id=..., path=...)`。

## 审计

审计回答的是「谁在什么时候用什么版本做了什么」，与面向排障的日志分开。

### 应用侧的 Key 解密自检

启动时逐条试解 `model_provider.api_key`，解不开的逐个点名。`AUTH_SECRET` 换了之后，受影响的表现只是「上游 401 + 后台显示未设置」，没人会想到是密钥换了；启动日志是唯一能直接说出后者的地方：

```text
有 N 个提供商的 API Key 解密失败（AUTH_SECRET 可能已更换）：...
```

`/health/ready` 里也有同样信息（`provider_keys` 项）。换 `AUTH_SECRET` 不需要重建库：到管理后台重新填写密钥即可。

### quality 平台的审计

`quality/` 的审计是 **append-only 的 JSONL**，一行一条，只追加不覆盖——覆盖式日志在争议时等于没有日志。每行记录 `actor`、`tenant`、`action`、`subject`、`outcome` 与 `detail`。

```bash
# 看审计流水
tail -n 20 quality/runs/core-behavior/<run_id>/audit.jsonl
```

## 质量报告产物

评测/调优/安全扫描的产物固定落 `quality/runs/<suite>/<run_id>/`：

| 文件 | 内容 |
|---|---|
| `report.json` | 运行报告：指标、逐任务汇总、指纹、成本、耗时 |
| `report.html` | 自包含单文件 HTML 报告，零外链，可直接分发 |
| `verdicts.jsonl` | 逐样本轨迹与判分结果 |
| `audit.jsonl` | append-only 审计 |

安全扫描的产物在 `quality/runs/security/<run_id>/security.json`；调优闭环的产物在 `quality/runs/tuning/<run_id>/tuning.json`。

报告记录了**指纹**：suite 指纹（用的是哪一版用例）与目标指纹（提示词 sha + 驱动 + 工具集 + 步数上限 + 温度）。指纹不同意味着两份报告不可比——门禁会把 suite 指纹不一致判成不放行。

报告还在 `report.config` 里记 `partial` 标记：预算（token / 美元 / 墙钟）耗尽时运行会提前停，写 `partial: true` 与 `stopped_reason`，门禁据此拒绝放行。

指标口径与调优闭环的完整说明见 [质量平台](../design/quality-platform.md)。

## 建议告警项

| 告警 | 判据 | 严重级建议 |
|---|---|---|
| 实例不就绪 | `/health/ready` 返回 503 持续 1 分钟 | 严重 |
| 对话错误率上升 | `chat_streams_total{outcome="error"}` 与 `{outcome="timeout"}` 占比上升 | 严重 |
| 请求被服务端拒绝 | `http_requests_total{status="5xx"}` 上升（对话的上游失败不走 5xx，走 `chat_streams_total`） | 严重 |
| 首字节变慢 | `chat_first_event_seconds` 的 p95 明显抬高 | 警告 |
| 模型容灾频繁 | `model_fallbacks_total` 增速上升 | 警告 |
| 限流降级 | `redis_unavailable_total` 增长 | 警告（配额会乘以实例数） |
| 工具失败率上升 | `tool_executions_total{outcome="failed"}` 占比上升 | 警告 |
| 回收失败 | 日志里出现 `回收：销毁失败` 或 `回收：租约卡在 provisioning` | 警告（容量位会一直被占） |
| 压缩频繁 | `context_compactions_total` 增速上升 | 提示 |
| API Key 解不开 | 启动日志的 `API Key 解密失败` | 严重 |
| 预算中断 | 报告里 `report.config.partial` 为 true | 严重（结论不可用） |

## 相关源码

- `app/api/ops.py`：三个运维端点
- `app/observability.py`：请求 ID 与 HTTP 指标中间件
- `app/services/metrics.py`：指标注册表、桶边界与指标名
- `app/config/logging_setup.py`：日志格式与第三方噪音抑制
- `app/db/maintenance.py`：API Key 解密审计与过期会话清理
- `quality/audit.py`：append-only 审计
- `quality/runner/report.py`：JSON / HTML / verdicts 产物

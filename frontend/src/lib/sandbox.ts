/**
 * 隔离执行环境（sandbox）的客户端。
 *
 * 两件事分成两条请求是刻意的：心跳只证明「页面还有人看」，服务端拿它刷 Redis 的 TTL；
 * 状态查询是给界面看的租约快照。合成一条的话，任何一次状态探测失败都会顺带漏掉一次
 * 续命，机器被回收的原因会变得无从解释。
 *
 * 响应形状见 `app/services/sandbox/lease.py`（租约状态机）与后端的心跳 / 状态端点。
 */

import { apiFetch } from './api';
import { isSessionIdSendable } from './session-id';

/**
 * 心跳周期。
 *
 * 与后端 `sandbox_heartbeat_seconds` 的默认值一致：服务端按「心跳 / 最近活动」距今超过
 * `idle_ttl`（默认 10 分钟）回收机器，30s 一次既留足冗余，又不至于把 TTL 写成轮询。
 */
export const SANDBOX_HEARTBEAT_INTERVAL_MS = 30_000;

export interface SandboxStatus {
  /**
   * 租约状态：`provisioning` / `ready` / `paused` / `destroying` / `destroyed` / `failed` /
   * `destroy_failed`。
   *
   * 用 string 而不是联合类型：后端新增状态时，前端不该把它当成「没有状态」丢掉——
   * 那会让一个真实存在的机器在界面上显示成「从未创建」。
   */
  state: string;
  /** 租约到期时间（服务端给的字符串）；没给就是 null，前端不自己推算。 */
  expires_at: string | null;
  /** 机器销毁时导出的产物地址；还在跑或没有产物时为 null。 */
  artifact_uri: string | null;
}

/**
 * 发一次心跳。
 *
 * 失败只留一行 debug 日志：心跳是后台续命，它失败时用户没有任何可做的事，而一个每 30s
 * 弹一次的「心跳失败」提示只会把真正要看的错误挤走。机器最终被回收也无害——下一次用到
 * 沙箱工具时会重新建机。
 *
 * 走 `apiFetch` 而不是裸 `fetch`：它带 cookie（`credentials: 'include'`）、拼基址，并在
 * 会话过期时参与全局的退回登录页处理。
 */
export async function sendSandboxHeartbeat(sessionId: string): Promise<void> {
  // 形状不合法的 id 服务端会回 422：心跳没有重试与提示，发了也只是每 30s 一条噪音日志
  if (!isSessionIdSendable(sessionId)) return;
  try {
    const res = await apiFetch('/api/sandbox/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (!res.ok) console.debug(`沙箱心跳失败：HTTP ${res.status}`);
  } catch (err) {
    console.debug('沙箱心跳失败:', err);
  }
}

/**
 * 查询本会话的租约状态。
 *
 * 404 只表示**这个会话从来没有过环境**（或服务端没开沙箱）：已回收的租约仍然返回 200 +
 * `state: 'destroyed'`，界面才能说「已回收，产物在这里」。404 是正常状态而不是错误，
 * 返回 null 让调用方用同一条分支处理「还没开始」与「查不到」。id 形状不合法时不发请求：
 * 服务端会 422，而调用方拿到的结论与 404 一样（查不到），没必要多一条失败路径。
 */
export async function fetchSandboxStatus(sessionId: string): Promise<SandboxStatus | null> {
  if (!isSessionIdSendable(sessionId)) return null;

  const res = await apiFetch(
    `/api/sandbox/status?session_id=${encodeURIComponent(sessionId)}`,
  );
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  return parseSandboxStatus(await res.json());
}

/**
 * 把响应收窄成前端可用的形状。
 *
 * 不直接 `as SandboxStatus`：代理返回错误页或后端字段缺失时，一个断言就能让界面拿
 * `undefined` 去渲染。缺 `state` 视为「没有可用的状态」；另外两个字段缺失退化成 null。
 */
function parseSandboxStatus(data: unknown): SandboxStatus | null {
  if (!data || typeof data !== 'object') return null;
  const record = data as Partial<SandboxStatus>;
  if (typeof record.state !== 'string' || !record.state) return null;
  return {
    state: record.state,
    expires_at:
      typeof record.expires_at === 'string' && record.expires_at ? record.expires_at : null,
    artifact_uri:
      typeof record.artifact_uri === 'string' && record.artifact_uri ? record.artifact_uri : null,
  };
}

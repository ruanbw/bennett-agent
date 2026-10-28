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
  /**
   * 机器销毁时导出的产物**键**（不是可直接打开的 URL）。
   *
   * 形如 `sbx_abc/workspace.tar.gz`。要拿到能点的地址必须走
   * `GET /api/sandbox/artifacts`（预签名、有时效），前端不拼存储路径也不猜桶名。
   * 界面上的产物列表一律以那个端点为准，这个字段只用来判断「有没有产物值得去查」。
   */
  artifact_uri: string | null;
}

/**
 * 一个可下载的产物。
 *
 * 字段名是线上契约（snake_case），与 `/api/sandbox/artifacts` 的响应对齐。
 */
export interface SandboxArtifact {
  /** 文件名（已由服务端展开到工作目录层面，可直接显示）。 */
  name: string;
  size_bytes: number;
  /**
   * 预签名下载地址。
   *
   * **有时效**（同响应里的 `expires_at`），所以不能缓存进消息、也不能拼路径——过期后
   * 唯一的出路是重新请求这个端点。直接给浏览器用（`<a href>` / `window.open`）。
   */
  download_url: string;
  /** 这个 URL 的失效时刻（服务端给的字符串）；给它是因为界面要能解释「点了 403」。 */
  expires_at: string;
}

/** `/api/sandbox/artifacts` 的完整响应。 */
export interface SandboxArtifactsResult {
  state: SandboxStatus['state'] | null;
  artifacts: SandboxArtifact[];
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

/**
 * 查询「我的电脑」里能下载的产物。
 *
 * 与 `/status` 分开成两条请求：状态是**每次打开面板都要知道**的轻量快照，产物则带着
 * 预签名 URL（服务端要跟对象存储换一次签名），没必要为一次状态轮询跟着签一批链接。
 *
 * 404 与 `/status` 同义：这个会话没有机器（或服务端没开沙箱）→ 空列表。返回空列表而
 * 不是 null，调用方就不用为「没有机器」和「机器还在建、暂无产物」写两套分支。
 */
export async function fetchSandboxArtifacts(sessionId: string): Promise<SandboxArtifactsResult> {
  if (!isSessionIdSendable(sessionId)) return { state: null, artifacts: [] };

  const res = await apiFetch(
    `/api/sandbox/artifacts?session_id=${encodeURIComponent(sessionId)}`,
  );
  if (res.status === 404) return { state: null, artifacts: [] };
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  return parseSandboxArtifacts(await res.json());
}

/**
 * 收窄产物列表。
 *
 * 逐条守卫而不是整块丢弃：一条坏记录（服务端换存储实现、某个键名拼错）不该让用户的
 * 其余产物一起从界面上消失。但 `download_url` 缺失的那条**必须丢**——没有地址的条目
 * 点了只会什么也不发生，比不显示更糟。
 */
function parseSandboxArtifacts(data: unknown): SandboxArtifactsResult {
  if (!data || typeof data !== 'object') return { state: null, artifacts: [] };
  const record = data as { state?: unknown; artifacts?: unknown };

  const artifacts: SandboxArtifact[] = [];
  if (Array.isArray(record.artifacts)) {
    for (const entry of record.artifacts) {
      if (!entry || typeof entry !== 'object') continue;
      const raw = entry as Partial<SandboxArtifact>;
      const name = typeof raw.name === 'string' && raw.name ? raw.name : undefined;
      const downloadUrl =
        typeof raw.download_url === 'string' && raw.download_url ? raw.download_url : undefined;
      if (name === undefined || downloadUrl === undefined) continue;
      artifacts.push({
        name,
        // 大小缺失/为负时退化 0：界面显示「0 B」比显示 NaN 或 undefined 更容易理解
        size_bytes:
          typeof raw.size_bytes === 'number' && Number.isFinite(raw.size_bytes) && raw.size_bytes >= 0
            ? raw.size_bytes
            : 0,
        download_url: downloadUrl,
        expires_at: typeof raw.expires_at === 'string' ? raw.expires_at : '',
      });
    }
  }

  return {
    state: typeof record.state === 'string' && record.state ? record.state : null,
    artifacts,
  };
}

/**
 * 租约状态的界面文案。
 *
 * 未知状态**不丢弃、也不显示成「未创建」**：后端新增一档时，一台真实存在的机器被显示成
 * 「从未创建」会让用户以为数据没了。这里把它如实说成「状态未知」，并把原始值带上——
 * 排障的人一眼就能看出是前端还没跟上，而不是机器真的没了。
 */
export function describeSandboxState(state: string | null | undefined): string {
  switch (state) {
    case 'provisioning':
      return '正在启动';
    case 'ready':
      return '已就绪';
    case 'paused':
      return '已暂停';
    case 'destroying':
      return '正在回收';
    case 'destroyed':
      return '已回收';
    case 'failed':
      return '启动失败';
    case 'destroy_failed':
      return '回收失败';
    // 没查到状态 ≠ 机器不存在：可能是还没建、也可能是这次探测失败了
    case null:
    case undefined:
    case '':
      return '未知';
    default:
      return `状态未知（${state}）`;
  }
}

/**
 * 产物大小。
 *
 * 用 1024 进制并保留一位小数：这里的用户在看自己机器上的文件，`1.5 MB` 比 `1572864`
 * 更容易判断「要不要下载」。0 字节单独说「空文件」，`0 B` 看着像没算出来。
 */
export function formatSandboxArtifactSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '未知大小';
  if (bytes === 0) return '空文件';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'] as const;
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  // 整数大小不摆一个没意义的 `.0`
  const rounded = Math.round(value * 10) / 10;
  const text = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  return `${text} ${units[unitIndex]}`;
}

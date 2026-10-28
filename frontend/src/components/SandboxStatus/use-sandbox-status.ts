/**
 * 「我的电脑」状态轮询。
 *
 * ## 为什么是轮询
 *
 * 状态会**自己**变（启动完成、闲置回收），但变化的时间点只有服务端知道。用低频轮询
 * 拿快照是这里最省的办法：机器闲置 10 分钟才回收，20s 一次的代价可以忽略，而用户
 * 不必手动刷新就能看到「已就绪」。
 *
 * ## 为什么面板收起时不停表，但降频
 *
 * 停表的话，用户重新打开面板要先等一个周期才看到真实状态（那期间显示的是几分钟前的
 * 旧值）。保留一个更慢的节拍既保持新鲜，又不至于在后台每 20s 打一次接口。
 *
 * ## 失败不弹错误
 *
 * 这块是**装饰性信息**：查不到不代表对话不能继续。失败只在指示器上变成「暂时查不到」，
 * 下一次轮询自然会把状态带回来。
 *
 * ## 为什么「加载中」是算出来的，不是 effect 里 setState 出来的
 *
 * 在 effect 里同步 `setLoading(true)` 会立刻触发第二轮渲染（且换会话时要多一次往返）。
 * 更实际的问题是**旧会话的数据不能显示在新会话上**：`loaded.sessionKey` 与当前会话
 * 不一致时，正确结论是「正在加载」，而这完全可以在渲染期直接算出来。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  fetchSandboxArtifacts,
  fetchSandboxStatus,
  type SandboxArtifact,
  type SandboxArtifactsResult,
  type SandboxStatus,
} from '../../lib/sandbox';

/** 面板展开时的刷新周期：够快看到「正在启动 → 已就绪」，又不至于刷成日志。 */
export const SANDBOX_STATUS_POLL_MS = 20_000;
/** 面板收起后的慢节拍：仍然保持新鲜，但不必按展开时的频率打接口。 */
export const SANDBOX_STATUS_IDLE_POLL_MS = 120_000;

export interface SandboxStatusState {
  status: SandboxStatus | null;
  artifacts: SandboxArtifact[];
  /** 最近一次探测是否失败（成功即清空）。 */
  failed: boolean;
  /** 首次加载中（此时不该把「还没有环境」当成结论显示）。 */
  loading: boolean;
  refresh: () => void;
}

/** 一次探测的结果，连同它属于哪个会话。 */
interface LoadedSnapshot {
  sessionKey: string;
  status: SandboxStatus | null;
  artifacts: SandboxArtifact[];
  failed: boolean;
}

/** 一次探测的原始结果（两条请求各自的成败）。 */
export interface SandboxProbe {
  status: SandboxStatus | null;
  artifacts: SandboxArtifactsResult | null;
  failed: boolean;
}

/**
 * 把两条请求的结果合成一份界面状态（纯函数，可单测）。
 *
 * 三条优先级，每一条都是为了不让一块失败把另一块也拖下水：
 *
 * 1. `/status` 成功就用它；
 * 2. `/status` 失败但 `/artifacts` 成功 → 用后者回显的 `state` 补一份最小状态。
 *    后端在 `/artifacts` 里**也**回了 `state`（两个端点都从最近一条租约读）。不用它的话，
 *    一台已回收、有文件可下载的机器会退化成「还没有启动」——用户会以为机器和产物都没了；
 * 3. 只有**两条都失败**才算「探测失败」。一条成功就说明服务端是通的。
 *
 * 产物列表只有 `/artifacts` 一条来源：`/status` 的 `artifact_uri` 是对象键，
 * 拿它去猜下载地址是错的（预签名必须由服务端签）。
 */
export function mergeSandboxProbe(
  statusResult: { ok: true; value: SandboxStatus | null } | { ok: false },
  artifactsResult:
    | { ok: true; value: SandboxArtifactsResult }
    | { ok: false },
): SandboxProbe {
  const artifacts = artifactsResult.ok ? artifactsResult.value : null;

  let status: SandboxStatus | null = statusResult.ok ? statusResult.value : null;
  if (status === null && artifacts?.state) {
    status = { state: artifacts.state, expires_at: null, artifact_uri: null };
  }

  return {
    status,
    artifacts,
    failed: !statusResult.ok && !artifactsResult.ok,
  };
}

/**
 * 拉一次状态与产物，并交给 `mergeSandboxProbe` 合成。
 *
 * 两条请求用 `allSettled`：一条失败不该让另一条的结果被丢掉。
 */
async function loadSandbox(sessionId: string): Promise<Omit<LoadedSnapshot, 'sessionKey'>> {
  const [statusResult, artifactsResult] = await Promise.allSettled([
    fetchSandboxStatus(sessionId),
    fetchSandboxArtifacts(sessionId),
  ]);

  if (statusResult.status === 'rejected') {
    console.debug('查询我的电脑状态失败:', statusResult.reason);
  }
  if (artifactsResult.status === 'rejected') {
    console.debug('查询我的电脑产物失败:', artifactsResult.reason);
  }

  const merged = mergeSandboxProbe(
    statusResult.status === 'fulfilled'
      ? { ok: true, value: statusResult.value }
      : { ok: false },
    artifactsResult.status === 'fulfilled'
      ? { ok: true, value: artifactsResult.value }
      : { ok: false },
  );

  return {
    status: merged.status,
    artifacts: merged.artifacts?.artifacts ?? [],
    failed: merged.failed,
  };
}

/**
 * @param sessionId 当前会话 id；`null` 时不发任何请求
 * @param enabled 后端是否开了沙箱（`/api/capabilities` 的 `sandbox`）；false 时不探测
 * @param active 面板是否展开（决定轮询频率）
 */
export function useSandboxStatus(
  sessionId: string | null,
  enabled: boolean,
  active: boolean,
): SandboxStatusState {
  const [loaded, setLoaded] = useState<LoadedSnapshot | null>(null);
  /** 手动刷新令牌：变化即立刻重新探测（同时复位轮询计时）。 */
  const [attempt, setAttempt] = useState(0);
  /** 上一次请求的会话 id：切会话时用来丢弃在途结果。 */
  const requestSessionRef = useRef<string | null>(null);

  const sessionKey = sessionId ?? '';
  const probing = Boolean(sessionId) && enabled;
  // 数据属于当前会话才算「已加载」；换会话的瞬间旧数据立刻失效，显示为加载中
  const fresh = loaded !== null && loaded.sessionKey === sessionKey;

  useEffect(() => {
    // 没开沙箱 / 还没选中会话：什么也不做（整块 UI 本来也不渲染）
    if (!sessionId || !enabled) return undefined;

    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    requestSessionRef.current = sessionId;

    // 用 setTimeout 串行排下一轮而不是 setInterval：慢网络下 setInterval 会积累，
    // 同一时刻多个在途请求依次回来时状态会来回跳。
    const schedule = (delay: number): void => {
      if (disposed) return;
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => void tick(), delay);
    };

    const tick = async (): Promise<void> => {
      const result = await loadSandbox(sessionId);
      if (disposed) return;
      // 切走了会话：这批结果属于上一个会话，不能覆盖当前显示
      if (requestSessionRef.current !== sessionId) return;
      setLoaded({ sessionKey: sessionId, ...result });
      schedule(active ? SANDBOX_STATUS_POLL_MS : SANDBOX_STATUS_IDLE_POLL_MS);
    };

    void tick();

    return () => {
      disposed = true;
      if (timer !== null) clearTimeout(timer);
    };
    // active / attempt 变化都重建轮询：前者换节拍，后者立刻补一次
  }, [sessionId, enabled, active, attempt]);

  const refresh = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);

  return {
    status: fresh ? loaded.status : null,
    artifacts: fresh ? loaded.artifacts : [],
    failed: fresh ? loaded.failed : false,
    loading: probing && !fresh,
    refresh,
  };
}

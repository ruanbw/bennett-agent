/**
 * 跨设备同步引擎：把本机变更推上去、把服务端变更拉下来。
 *
 * ## 形状
 *
 * 一个模块级单例，不依赖 React：登录后 `startSync(userId)`，登出时 `stopSync()`。
 * 状态通过订阅暴露给 UI（`subscribeStatus` / `subscribeApplied`），React 只做展示。
 *
 * 循环长这样：
 *
 *   push 掉所有 pending → pull（可见时长轮询 25s；隐藏时立即返回）
 *        ↑                                   │
 *        └──────────── has_more 就继续 ────────┘
 *
 * ## 三条必须守住的规则
 *
 * 1. **远端写入不能触发本地标记**。应用拉下来的行必须走 `runSyncInternal`，否则会被
 *    表级 hook 标成 pending，然后又被推上去 —— 拉/推无限循环。
 * 2. **pending 的行不许被拉到的东西覆盖**（见 `shouldApplyRemote`）。本机还有没推上去
 *    的内容时，服务端那份是旧的。
 * 3. **失败要能被看见**。连续失败只在 console 里打日志等于没发生：`status.lastError`、
 *    `status.rejected` 与 `status.rejectedReason` 都会挂到侧边栏的同步面板上，面板同时
 *    提供「立即同步」与「重新上传被拒内容」两个恢复入口（`retryNow` / `retryRejected`）。
 *
 * ## 为什么不是「每次写库都推一次」
 *
 * 流式生成期间 `saveProgress` 每 800ms 写一次助手消息。按写入触发推送会把中间态反复
 * 上传；所以表级 hook 只负责**打标记**，推送由明确的时机触发：一轮结束、标签页隐藏、
 * 恢复在线、启动、以及用户改动会话（重命名/置顶/删除）。
 */

import { db, runSyncInternal } from '../../db';
import type { MessageRecord, SessionRecord } from '../../types';
import { notifySessionsChanged } from '../multi-tab';
import { type PullResponse, type PushResponse, pullRows, pushRows, SyncHttpError } from './api';
import { retryDelayMs } from '../retry-after';
import {
  PULL_TIMEOUT_RETRY_MS,
  createTimeoutStreak,
  isPullTimeout,
} from './long-poll';
import { buildPushBatch, fromConversationRow, fromMessageRow, shouldApplyRemote, type PushBatch } from './mapping';
// 只取运行时函数；status-view 对 engine 只有 `import type`，不构成运行时循环依赖
import { describeSyncFailure } from './status-view';

/** 本地连续写入（标题占位 → 标题定稿 → 终态落库）合并成一个推送窗口。 */
const PUSH_DEBOUNCE_MS = 400;
/** 单次 push 的行数上限，与后端 `SYNC_PUSH_MAX_ROWS`（默认 200）留余量。 */
const PUSH_BATCH_ROWS = 150;
/** 单次 pull 每个数据流的行数上限；服务端还会按字节再截一次。 */
const PULL_LIMIT = 300;
/** 长轮询挂起时长，需小于后端默认 25s 以便「无变化」时也能按期返回。 */
const LONG_POLL_SECONDS = 25;
const RETRY_MIN_MS = 2_000;
const RETRY_MAX_MS = 60_000;
const CURSOR_KEY = 'cursor';
const BACKFILL_KEY = 'backfill';

export interface SyncStatus {
  /** 引擎是否在跑（登录后 true）。 */
  running: boolean;
  /** 本机**还会重试**的待推送行数（会话 + 消息）。被拒收的另算在 `rejected`。 */
  pending: number;
  /** 被服务端明确拒绝、停在那里的行数（超长 / 超配额）。 */
  rejected: number;
  /** 最近一次拒绝原因码（`too_large` / `quota_exceeded` / `conversation_missing`）。 */
  rejectedReason: string | null;
  /** 推送请求是否在飞：面板据此说「正在上传」而不是「等待上传」。 */
  syncing: boolean;
  /** 最近一次失败的可读原因；成功一轮后清空。 */
  lastError: string | null;
  /** 最近一次成功同步完成的时间（毫秒）；从未成功过为 null。 */
  lastSyncedAt: number | null;
  /**
   * 本次会话里有多少行本机修改被服务器上更新版本覆盖（推送被判 `stale`）。
   *
   * `stale` 的处理是「标 synced，等下一轮 pull 用服务端版本盖掉本地」——本机那次修改
   * 就此丢掉。静默丢弃是最糟的失败方式（用户以为改上去了），所以单独计数给面板显示。
   */
  superseded?: number;
}

const IDLE_STATUS: SyncStatus = {
  running: false,
  pending: 0,
  rejected: 0,
  rejectedReason: null,
  syncing: false,
  lastError: null,
  lastSyncedAt: null,
  superseded: 0,
};

let status: SyncStatus = { ...IDLE_STATUS };
const statusListeners = new Set<() => void>();
const appliedListeners = new Set<(sessionIds: string[]) => void>();

let userId: string | null = null;
let started = false;
/** 每次 start/stop 递增：所有 async 结果回来时先核对它，避免旧循环写新账号的数据。 */
let generation = 0;
let cursor: string | null = null;
let pushTimer: ReturnType<typeof setTimeout> | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let backoffMs = RETRY_MIN_MS;
/** 长轮询超时的连续计数：单次静默重试，连续多次才染色（见 long-poll.ts）。 */
const pullTimeouts = createTimeoutStreak();
let pumpRunning = false;
let pushInFlight: Promise<number> | null = null;
/** 推送在飞计数：并发调用共享同一个 Promise，但每个调用方都必须配平自己的 +1/-1。 */
let pushActivity = 0;

// ---------------------------------------------------------------------
// 订阅
// ---------------------------------------------------------------------

export function getStatus(): SyncStatus {
  return status;
}

export function subscribeStatus(listener: () => void): () => void {
  statusListeners.add(listener);
  return () => {
    statusListeners.delete(listener);
  };
}

/** 同标签页的「拉到了新内容」通知。跨标签页的走 `notifySessionsChanged`。 */
export function subscribeApplied(listener: (sessionIds: string[]) => void): () => void {
  appliedListeners.add(listener);
  return () => {
    appliedListeners.delete(listener);
  };
}

function setStatus(patch: Partial<SyncStatus>): void {
  status = { ...status, ...patch };
  for (const listener of [...statusListeners]) listener();
}

// ---------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------

export function startSync(id: string): void {
  if (started && userId === id) return;
  stopSync();
  userId = id;
  started = true;
  generation += 1;
  const gen = generation;
  setStatus({ running: true, lastError: null });
  attachBrowserListeners();

  void (async () => {
    try {
      const storedCursor = await loadCursor(id);
      if (!isCurrent(gen)) return;
      cursor = storedCursor;
      await backfillIfNeeded(id);
      if (!isCurrent(gen)) return;
      await refreshQueue();
      if (!isCurrent(gen)) return;
      void pump(gen);
    } catch (err) {
      if (isCurrent(gen)) reportError(err);
    }
  })();
}

export function stopSync(): void {
  started = false;
  generation += 1;
  userId = null;
  cursor = null;
  if (pushTimer !== null) clearTimeout(pushTimer);
  if (retryTimer !== null) clearTimeout(retryTimer);
  pushTimer = null;
  retryTimer = null;
  backoffMs = RETRY_MIN_MS;
  pullTimeouts.reset();
  detachBrowserListeners();
  setStatus({ ...IDLE_STATUS });
}

function isCurrent(gen: number): boolean {
  return started && generation === gen;
}

function isVisible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

// ---------------------------------------------------------------------
// 浏览器事件
// ---------------------------------------------------------------------

function onVisibilityChange(): void {
  if (!started) return;
  if (isVisible()) {
    // 回到前台：先补推（隐藏期间的写入可能还没上去），再继续长轮询
    requestPush(0);
    void pump(generation);
  }
}

function onOnline(): void {
  if (!started) return;
  backoffMs = RETRY_MIN_MS;
  // 网络状态刚变过：之前的超时不再代表当前网络，重新按「偶发」计数
  pullTimeouts.reset();
  requestPush(0);
  void pump(generation);
}

function attachBrowserListeners(): void {
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibilityChange);
  if (typeof window !== 'undefined') window.addEventListener('online', onOnline);
}

function detachBrowserListeners(): void {
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibilityChange);
  if (typeof window !== 'undefined') window.removeEventListener('online', onOnline);
}

// ---------------------------------------------------------------------
// 触发
// ---------------------------------------------------------------------

/** 请求一次推送（合并窗口）。引擎没在跑时是空操作。 */
export function requestPush(delayMs: number = PUSH_DEBOUNCE_MS): void {
  if (!started) return;
  if (pushTimer !== null) clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    pushTimer = null;
    void kickPush();
  }, delayMs);
}

/** 请求一次拉取（例如切换会话、回前台）。 */
export function requestPull(): void {
  if (!started) return;
  void pump(generation);
}

async function kickPush(): Promise<void> {
  const gen = generation;
  try {
    await pushUntilDrained(gen);
    if (isCurrent(gen)) await refreshQueue();
  } catch (err) {
    if (!isCurrent(gen)) return;
    reportError(err);
    scheduleRetry(gen, err);
  }
}

// ---------------------------------------------------------------------
// 主循环
// ---------------------------------------------------------------------

async function pump(gen: number): Promise<void> {
  if (pumpRunning || !isCurrent(gen)) return;
  pumpRunning = true;
  // 出错时要能区分「卡在长轮询」与「推不上去」：只有前者允许静默重试
  let phase: 'push' | 'pull' = 'push';
  try {
    while (isCurrent(gen)) {
      phase = 'push';
      await pushUntilDrained(gen);
      if (!isCurrent(gen)) return;

      phase = 'pull';
      const hasMore = await pullOnce(gen, isVisible() ? LONG_POLL_SECONDS : 0);
      if (!isCurrent(gen)) return;
      if (hasMore) continue;
      // 隐藏时不长轮询：挂起一个 25s 的请求只会在切回来时立刻作废
      if (!isVisible()) return;
      // 成功走过一轮：清掉错误与退避
      backoffMs = RETRY_MIN_MS;
      if (status.lastError !== null) setStatus({ lastError: null });
    }
  } catch (err) {
    if (!isCurrent(gen)) return;

    /*
      长轮询超时 ≠ 同步失败。

      pull 是 25s 的长轮询，客户端给 40s；超过 40s 的典型原因是浏览器把标签页冻结/节流了
      （定时器在冻结期间不走，恢复时立刻到期），而不是服务器挂了——真正的断网/服务端不可达
      会很快抛 TypeError 或拿到 5xx。把单次超时染红状态面板会误报「同步失败」，用户以为数据
      没保存，而下一轮轮询其实就成功了。连续多次才值得上报（见 lib/sync/long-poll.ts）。
    */
    if (phase === 'pull' && isPullTimeout(err) && !pullTimeouts.record()) {
      console.info('同步长轮询超时，静默重试（多半是标签页被冻结或网络栈卡顿）');
      // 固定短延迟重试：超时不该放大退避，否则冻结几次后同步就慢到形同停摆
      scheduleTimeoutRetry(gen);
      return;
    }

    reportError(err);
    scheduleRetry(gen, err);
  } finally {
    pumpRunning = false;
    // 旧世代结束（换账号/登出）时，新世代的 pump 可能因为 pumpRunning 被吞掉。
    // 这里补一次：宁可多跑一次 pump（它自己会判 isCurrent），也不要让同步静默停摆。
    if (started && generation !== gen) void pump(generation);
  }
}

/** 服务端 `Retry-After` 最多听 5 分钟：异常大的头不能把同步挂死，但 17s 这类要照等。 */
const RETRY_AFTER_MAX_MS = 5 * 60_000;

function scheduleRetry(gen: number, err: unknown): void {
  if (!isRetryable(err) || !isCurrent(gen)) return;
  if (retryTimer !== null) clearTimeout(retryTimer);
  const retryAfterSeconds = err instanceof SyncHttpError ? err.retryAfterSeconds : null;
  const delay = retryDelayMs(retryAfterSeconds, backoffMs, RETRY_AFTER_MAX_MS);
  backoffMs = Math.min(backoffMs * 2, RETRY_MAX_MS);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void pump(gen);
  }, delay);
}

/**
 * 长轮询超时后的重试：固定短延迟，**不碰退避**。
 *
 * 走 `scheduleRetry` 会让 backoff 每次翻倍：标签页被冻结几次之后就变成 60s 才发一次
 * 请求，用户切回来看到「已同步」却再也不同步了。超时只是这一跳卡住，短暂停顿即可。
 */
function scheduleTimeoutRetry(gen: number): void {
  if (!isCurrent(gen)) return;
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void pump(gen);
  }, PULL_TIMEOUT_RETRY_MS);
}

/** 4xx 里除了游标/限流都是「请求本身不对」：重试只会一直撞同一面墙。 */
function isRetryable(err: unknown): boolean {
  if (err instanceof SyncHttpError) return !err.isFatal;
  return true;
}

function reportError(err: unknown): void {
  // 面板上显示的是翻译后的中文结论，不是 `err.message`：长轮询超时会拿到英文
  // 「signal timed out」，HTTP 失败会漏出「HTTP 500」，两种都不告诉用户能做什么。
  console.warn('同步失败:', err);
  setStatus({ lastError: describeSyncFailure(err) });
}

// ---------------------------------------------------------------------
// push
// ---------------------------------------------------------------------

async function pushUntilDrained(gen: number): Promise<number> {
  let total = 0;
  // 轮数上限只是兜底：正常情况下每轮要么推进、要么把行标成 error，不会原地打转。
  // 有它的话，将来任何一种新的拒绝原因都不可能把整条同步链路卡死在这里。
  for (let round = 0; round < 50 && isCurrent(gen); round += 1) {
    const pushed = await pushBatch();
    total += pushed;
    if (pushed === 0) break;
  }
  return total;
}

/**
 * 推一批。返回本批处理的行数；0 表示没有待推内容。
 *
 * 并发调用共享同一个在飞 Promise：长轮询唤醒、用户操作、可见性事件可能同时想推，
 * 各推一批会互相把对方的行重复发一遍。
 */
async function pushBatch(): Promise<number> {
  if (pushInFlight !== null) return pushInFlight;
  beginPushActivity();
  pushInFlight = (async () => {
    const [sessions, messages] = await readPending();
    const batch = buildPushBatch(sessions, messages, PUSH_BATCH_ROWS);
    if (batch.conversations.length === 0 && batch.messages.length === 0) return 0;

    const response = await pushRows({
      conversations: batch.conversations,
      messages: batch.messages,
    });
    await ackPush(batch, response);
    return batch.conversations.length + batch.messages.length;
  })();
  try {
    return await pushInFlight;
  } finally {
    pushInFlight = null;
    endPushActivity();
  }
}

function beginPushActivity(): void {
  pushActivity += 1;
  if (pushActivity === 1) setStatus({ syncing: true });
}

function endPushActivity(): void {
  pushActivity = Math.max(0, pushActivity - 1);
  if (pushActivity === 0) setStatus({ syncing: false });
}

async function readPending(): Promise<[SessionRecord[], MessageRecord[]]> {
  return db.transaction('r', db.sessions, db.messages, async () => {
    const sessions = await db.sessions.where('syncState').equals('pending').toArray();
    const messages = await db.messages.where('syncState').equals('pending').toArray();
    return [sessions, messages] as [SessionRecord[], MessageRecord[]];
  });
}

/**
 * 处理服务端的逐行结果。
 *
 * - 接受 → 标 synced；
 * - `stale` / `not_owned` / `conversation_deleted` → 也标 synced：这几行服务端已有
 *   更新版本或本来就不该存在，重试永远不会成功；
 * - `conversation_missing` → 消息留着 pending，把会话重新标 pending（下一批先推会话）；
 * - 其余（`too_large` / `quota_exceeded`）→ 标 error 并计数，重试没有意义。
 */
async function ackPush(batch: PushBatch, response: PushResponse): Promise<void> {
  const rejections = new Map(response.rejected.map((row) => [`${row.kind}:${row.id}`, row.reason]));
  let rejectedRows = 0;
  let rejectionReason: string | null = null;

  await runSyncInternal(async () => {
    for (const row of batch.conversations) {
      const reason = rejections.get(`conversation:${row.id}`);
      if (await settleRow('conversation', row.id, row.client_rev, reason)) {
        rejectedRows += 1;
        rejectionReason ??= reason ?? null;
      }
    }
    for (const row of batch.messages) {
      const reason = rejections.get(`message:${row.id}`);
      if (reason === 'conversation_missing') {
        const conversationRejected = rejections.get(`conversation:${row.conversation_id}`);
        const conversation = await db.sessions.get(row.conversation_id);
        if (!conversationRejected && conversation && !conversation.deleted) {
          // 会话在本批里没带上去（被截断或在写库后才出现）：下一批先推会话，消息保持 pending。
          await db.sessions.update(row.conversation_id, { syncState: 'pending' });
          continue;
        }
        // 会话根本不存在（或已被服务端判死）：这条消息永远不会成功，停下来并报错，
        // 否则每轮都会「标 pending → 再被判 missing」，形成死循环。
        await db.messages.update(row.id, { syncState: 'error', syncError: reason });
        rejectedRows += 1;
        rejectionReason ??= reason;
        continue;
      }
      if (await settleRow('message', row.id, row.client_rev, reason)) {
        rejectedRows += 1;
        rejectionReason ??= reason ?? null;
      }
    }
  });

  // 只记原因，数量由 `refreshQueue` 从库里重算——增量计数在「同一行被重新标记」时
  // 会漂移，而重算永远不会。
  if (rejectionReason !== null) setStatus({ rejectedReason: rejectionReason });
}

/**
 * 结算一行。返回该行是否停在 error。
 *
 * **必须先核对修订号**：`pushBatch` 读的是快照，HTTP 往返期间用户完全可能又改了这行
 * （保存完整回答、重命名、甚至删掉它）。按 id 无条件写 `synced` 会把那次改动标成
 * 「已同步」，而它其实从没发出去过——本地与服务端从此永久不一致，且因为本地修订号
 * 更大，拉取也不会把它改回来。删会话就是这么丢的。
 */
async function settleRow(
  kind: 'conversation' | 'message',
  id: string,
  pushedRev: number,
  reason: string | undefined,
): Promise<boolean> {
  const table = kind === 'conversation' ? db.sessions : db.messages;
  const local = await table.get(id);
  if (!local) return false;
  if ((local.clientRev ?? 0) !== pushedRev) {
    // 在途期间又改过：保持 pending，下一批会把更新的版本推上去。
    return false;
  }
  if (reason === undefined || reason === 'stale' || reason === 'not_owned' || reason === 'conversation_deleted') {
    // 顺带清掉上一次的拒收原因：这一行已经不再停在 error 了。
    await table.update(id, { syncState: 'synced', syncError: null });
    if (reason === 'stale') {
      // 服务端有更新的修订号：这一行本机修改即将被下一轮 pull 覆盖。
      // 不静默吞掉——否则用户完全无从得知自己改的内容没上去。
      setStatus({ superseded: (status.superseded ?? 0) + 1 });
    }
    if (reason !== undefined) {
      console.warn(`同步行被服务端忽略 (${kind}=${id} reason=${reason})`);
    }
    return false;
  }
  await table.update(id, { syncState: 'error', syncError: reason ?? null });
  console.warn(`同步行被服务端拒绝 (${kind}=${id} reason=${reason})`);
  return true;
}

// ---------------------------------------------------------------------
// pull
// ---------------------------------------------------------------------

/** 返回是否还有下一页。 */
async function pullOnce(gen: number, timeoutSeconds: number): Promise<boolean> {
  let page: PullResponse;
  try {
    page = await pullRows(cursor, { timeoutSeconds, limit: PULL_LIMIT });
  } catch (err) {
    if (!(err instanceof SyncHttpError) || !err.isCursorReset) throw err;
    // 游标失效（服务端回滚 / 格式升级）：丢掉它，从头拉一次全量
    console.warn('同步游标失效，改为全量重新同步:', err.message);
    cursor = null;
    await saveCursor(null);
    // 服务端被回滚/换库时，本机那些「已同步」的行在服务端可能根本不存在。
    // 只重置游标会把它们永久留在本机，所以顺带标脏，让下一轮 push 补回去。
    await markAllPending();
    page = await pullRows(null, { limit: PULL_LIMIT });
  }
  // 成功拿到一页：偶发超时计数清零
  pullTimeouts.reset();
  if (!isCurrent(gen)) return false;

  const affected = await applyPage(page);
  cursor = page.cursor;
  await saveCursor(cursor);
  setStatus({ lastSyncedAt: Date.now() });
  if (affected.length > 0) notifyApplied(affected);
  await refreshQueue();
  return page.has_more;
}

/**
 * 把一页服务端数据应用进本地库（**不触发**同步标记）。
 *
 * 删除是单向的：服务端说这个会话没了，本地就算还有内容也一起删——否则下次推送会把它
 * 复活成一条新会话。
 */
async function applyPage(page: PullResponse): Promise<string[]> {
  const affected: string[] = [];

  await runSyncInternal(async () => {
    // 本页里被删掉的会话：消息循环必须跳过它们，否则会把刚删掉的消息再 put 回来。
    // 今天服务端删墓碑时会级联删消息，所以同一页不会同时出现两者；但这层顺序依赖
    // 不该由「服务端实现细节」保证。
    const deletedIds = new Set(page.conversations.filter((row) => row.deleted).map((row) => row.id));

    for (const row of page.conversations) {
      const local = await db.sessions.get(row.id);
      if (row.deleted) {
        await db.messages.where('sessionId').equals(row.id).delete();
        if (local) await db.sessions.delete(row.id);
        affected.push(row.id);
        continue;
      }
      if (!shouldApplyRemote(row, local)) continue;
      await db.sessions.put(fromConversationRow(row, local));
      affected.push(row.id);
    }

    for (const row of page.messages) {
      if (deletedIds.has(row.conversation_id)) continue;
      const local = await db.messages.get(row.id);
      if (!shouldApplyRemote(row, local)) continue;
      await db.messages.put(fromMessageRow(row, local));
      affected.push(row.conversation_id);
    }
  });

  return [...new Set(affected)];
}

function notifyApplied(sessionIds: string[]): void {
  const unique = [...new Set(sessionIds.filter(Boolean))];
  for (const listener of [...appliedListeners]) listener(unique);
  // 其他标签页：让它们自己重读（BroadcastChannel 不回传给发送方）
  for (const id of unique) notifySessionsChanged(id);
}

// ---------------------------------------------------------------------
// 游标与回填
// ---------------------------------------------------------------------

async function loadCursor(id: string): Promise<string | null> {
  const record = await db.syncMeta.get(CURSOR_KEY);
  if (!record || record.userId !== id) return null;
  return record.value || null;
}

async function saveCursor(value: string | null): Promise<void> {
  if (!userId) return;
  await db.syncMeta.put({
    key: CURSOR_KEY,
    userId,
    value: value ?? '',
    updatedAt: Date.now(),
  });
}

/**
 * 首次同步：把本机已有内容全部标成待推送。
 *
 * 只做一次（用 `backfill` 标记）。换了账号要重做——游标和标记都是按 userId 校验的，
 * 登出时本地库本来也会被清空。
 */
/** 把本机所有行标成待推送（修订号不动：内容没变，只是「服务端可能没有」）。 */
async function markAllPending(): Promise<void> {
  await runSyncInternal(async () => {
    await db.sessions.toCollection().modify((session) => {
      session.syncState = 'pending';
      session.clientRev = session.clientRev ?? 0;
    });
    await db.messages.toCollection().modify((message) => {
      message.syncState = 'pending';
      message.clientRev = message.clientRev ?? 0;
    });
  });
}

async function backfillIfNeeded(id: string): Promise<void> {
  const done = await db.syncMeta.get(BACKFILL_KEY);
  if (done?.userId === id) return;

  await markAllPending();
  await db.syncMeta.put({ key: BACKFILL_KEY, userId: id, value: '1', updatedAt: Date.now() });
}

// ---------------------------------------------------------------------
// 给 UI / 登出流程用的查询
// ---------------------------------------------------------------------

/**
 * 尚未落到服务端的行数：`pending`（还会重试）+ `error`（服务端明确拒绝，需要用户处理）。
 *
 * error 必须一起算：它们同样只存在于本机，登出会一起清掉。只数 pending 会让确认框
 * 显示「0 条未同步」然后把拒收的内容删掉。
 */
export async function countUnsynced(): Promise<number> {
  const states = ['pending', 'error'] as const;
  let total = 0;
  for (const state of states) {
    total += await db.sessions.where('syncState').equals(state).count();
    total += await db.messages.where('syncState').equals(state).count();
  }
  return total;
}

export async function countPending(): Promise<number> {
  return countUnsynced();
}

/**
 * 从库里重算队列：`pending` 与 `rejected` 分开统计。
 *
 * 分开是 UI 的要求：两类行的含义完全不同——pending 会自己好，rejected 永远不会。
 * 合成一个数字后，角标会在被拒收后一直转圈，用户等多久都不会好。
 */
async function refreshQueue(): Promise<void> {
  try {
    const count = async (state: 'pending' | 'error'): Promise<number> =>
      (await db.sessions.where('syncState').equals(state).count()) +
      (await db.messages.where('syncState').equals(state).count());

    const [pending, rejected] = await Promise.all([count('pending'), count('error')]);
    const patch: Partial<SyncStatus> = {};
    if (pending !== status.pending) patch.pending = pending;
    if (rejected !== status.rejected) patch.rejected = rejected;

    // 原因从行上读，而不是只留在内存：刷新/重开页面后仍要能解释「为什么传不上去」。
    // 被拒的行被重试/删掉后原因自然消失，不会拿旧原因解释新状态。
    const rejectedReason =
      rejected === 0
        ? null
        : ((await db.messages.where('syncState').equals('error').first())?.syncError ??
          (await db.sessions.where('syncState').equals('error').first())?.syncError ??
          null);
    if (rejectedReason !== status.rejectedReason) patch.rejectedReason = rejectedReason;

    if (Object.keys(patch).length > 0) setStatus(patch);
  } catch (err) {
    console.warn('统计同步队列失败:', err);
  }
}

/** 立即重试一次（错误横幅与面板的行动入口）。不清 `lastError`：失败原因要留到真正成功。 */
export function retryNow(): void {
  if (!started) return;
  requestPush(0);
  requestPull();
}

/**
 * 把服务端拒收的行重新排进推送队列，返回重新入队的行数（不触发推送）。
 *
 * 抽出来是为了能在不启动同步引擎的情况下测：它只改本地标记，不碰网络。
 */
export async function requeueRejectedRows(): Promise<number> {
  let requeued = 0;
  await runSyncInternal(async () => {
    // 两张表分开写：`db.sessions` 与 `db.messages` 的 Table 类型不同，
    // 放进同一个循环会让 `modify` 的联合签名不可调用。
    requeued += await db.sessions.where('syncState').equals('error').count();
    await db.sessions
      .where('syncState')
      .equals('error')
      .modify({ syncState: 'pending', syncError: null });
    requeued += await db.messages.where('syncState').equals('error').count();
    await db.messages
      .where('syncState')
      .equals('error')
      .modify({ syncState: 'pending', syncError: null });
  });
  return requeued;
}

/**
 * 把服务端拒收的行重新排进推送队列，返回重新入队的行数。
 *
 * 拒收通常来自「单条过大」或「会话配额已满」——用户删掉旧内容、或服务端配额调整之后，
 * 同样的行就能推上去了。没有这个入口，那些行会永远停在 error，用户除了清空本机数据
 * 没有任何办法——而 UI 之前连它们存在都不说。
 */
export async function retryRejected(): Promise<number> {
  const requeued = await requeueRejectedRows();
  if (requeued > 0) {
    await refreshQueue();
    requestPush(0);
  }
  return requeued;
}

/**
 * 登出前最后一次推送，`timeoutMs` 到点就放弃（不阻塞用户退出）。
 *
 * 返回仍未能推送出去的行数：调用方据此决定要不要提醒用户「这些内容会丢」。
 */
export async function flushPending(timeoutMs = 2_000): Promise<number> {
  const deadline = Date.now() + timeoutMs;
  try {
    while (Date.now() < deadline) {
      const pushed = await pushBatch();
      if (pushed === 0) break;
    }
  } catch (err) {
    console.warn('登出前的最后一次同步失败:', err);
  }
  return countPending();
}

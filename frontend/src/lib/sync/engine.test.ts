/**
 * 同步引擎的推送结算（`ackPush`/`settleRow`）行为锁。
 *
 * 这里锁的是审查中发现的**丢删除**缺陷：`pushBatch` 先取 pending 快照，HTTP 往返期间
 * 用户完全可能又改了同一行。若回执按 id 无条件标 `synced`，那次改动就永远不会被推送，
 * 而且因为本地修订号更大，pull 也修不回来。
 *
 * 用 `fake-indexeddb` 跑真 Dexie，只把 HTTP 层（`./api`）替换成受控 Promise——
 * 需要精确控制「推送在飞」这个时间窗口。
 */

import 'fake-indexeddb/auto';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MessageRecord, SessionRecord } from '../../types';
import { db } from '../../db';

const mocks = vi.hoisted(() => ({ pushRows: vi.fn(), pullRows: vi.fn() }));

vi.mock('./api', () => ({
  pushRows: mocks.pushRows,
  pullRows: mocks.pullRows,
  PULL_TIMEOUT_MS: 1_000,
  SyncHttpError: class SyncHttpError extends Error {
    status: number;
    code: string | null = null;
    retryAfterSeconds: number | null = null;
    constructor(message = '', status = 0, code: string | null = null) {
      super(message);
      this.name = 'SyncHttpError';
      this.status = status;
      this.code = code;
    }
    get isCursorReset(): boolean {
      return false;
    }
    // 与真实实现同口径：413 是「砍半就能过」，不是「请求本身写错了」
    get isTooLarge(): boolean {
      return this.status === 413;
    }
    get isFatal(): boolean {
      return this.status >= 400 && this.status < 500;
    }
  },
}));

const { flushPending, getStatus, requeueRejectedRows, startSync, stopSync } = await import('./engine');
const { SyncHttpError } = await import('./api');

const session = (id: string): SessionRecord => ({ id, title: '标题', createdAt: 1, updatedAt: 1 });
const message = (id: string, sessionId: string): MessageRecord => ({
  id,
  sessionId,
  role: 'user',
  content: '你好',
  createdAt: 1,
});

const accepted = (version: number, conversations = 1, messages = 0) => ({
  version,
  accepted: { conversations, messages },
  rejected: [] as Array<{ kind: 'conversation' | 'message'; id: string; reason: string }>,
});

async function waitFor(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('waitFor 超时');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

/** 同上的异步版：断言的是库里/状态里的结果，而不是 mock 的调用次数。 */
async function waitForAsync(predicate: () => Promise<boolean>, timeoutMs = 2_000): Promise<void> {
  const started = Date.now();
  while (!(await predicate())) {
    if (Date.now() - started > timeoutMs) throw new Error('waitForAsync 超时');
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

/**
 * 长轮询的「挂住」替身。
 *
 * pump 是个 `while (true)`：如果 mock 立即返回，它会在用例里空转烧 CPU；而挂住不返回又会让
 * `pumpRunning` 一直为 true，把下一个用例的 pump 吃掉。所以存下 resolve，在 afterEach 里统一
 * 放行——那时 generation 已经变过，pump 会自己退出。
 */
const hangingPulls: Array<() => void> = [];

function hangPullUntilReleased(): Promise<unknown> {
  return new Promise((resolve) => {
    hangingPulls.push(() => resolve({ cursor: 'v1:99', has_more: false, conversations: [], messages: [] }));
  });
}

beforeEach(async () => {
  mocks.pushRows.mockReset();
  mocks.pullRows.mockReset();
  await db.sessions.clear();
  await db.messages.clear();
  await db.syncMeta.clear();
});

afterEach(async () => {
  stopSync();
  for (const release of hangingPulls.splice(0)) release();
  // 让被放行的 pump 跑完它的 finally（清 `pumpRunning`），再交给下一个用例
  await new Promise((resolve) => setTimeout(resolve, 0));
  vi.unstubAllGlobals();
});

describe('推送回执', () => {
  it('在途期间的删除不会被回执吞掉，下一批会带上墓碑', async () => {
    await db.sessions.add(session('s1'));

    let release: ((value: unknown) => void) | null = null;
    mocks.pushRows.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }),
    );

    const flushing = flushPending(5_000);
    await waitFor(() => mocks.pushRows.mock.calls.length === 1);

    // 推送在飞期间用户删除了这个会话（本地 rev+1、deleted=true）
    await db.sessions.update('s1', { deleted: true });

    mocks.pushRows.mockResolvedValueOnce(accepted(3));
    (release as ((value: unknown) => void) | null)?.(accepted(2));
    await flushing;

    const row = await db.sessions.get('s1');
    expect(row?.deleted).toBe(true);
    expect(row?.syncState).toBe('synced');

    // 第二次推送必须真的带上墓碑，而不是把行永久标成「已同步」
    const second = mocks.pushRows.mock.calls[1][0] as { conversations: Array<Record<string, unknown>> };
    expect(second.conversations[0]).toMatchObject({ id: 's1', deleted: true });
  });

  it('没有并发修改时照常标记已同步（不引入永久 pending）', async () => {
    await db.sessions.add(session('s1'));
    mocks.pushRows.mockResolvedValueOnce(accepted(1));

    await flushPending(2_000);

    expect((await db.sessions.get('s1'))?.syncState).toBe('synced');
    expect(mocks.pushRows).toHaveBeenCalledTimes(1);
  });

  it('会话不存在的消息被判 conversation_missing 后停在 error，不会无限重推', async () => {
    await db.messages.add(message('m1', 'sess_ghost'));
    mocks.pushRows.mockResolvedValue({
      version: 1,
      accepted: { conversations: 0, messages: 0 },
      rejected: [{ kind: 'message', id: 'm1', reason: 'conversation_missing' }],
    });

    await flushPending(2_000);

    expect((await db.messages.get('m1'))?.syncState).toBe('error');
    // 第二轮读到「没有 pending」就结束：不会每轮都把这条消息重发一次
    expect(mocks.pushRows).toHaveBeenCalledTimes(1);
  });
});

describe('重新上传被拒内容', () => {
  it('把两张表里停在 error 的行重新排进队列，修订号不变', async () => {
    await db.sessions.add({ ...session('s1'), syncState: 'error', clientRev: 4 });
    await db.messages.add({ ...message('m1', 's1'), syncState: 'error', clientRev: 2 });
    // 已同步的行不该被顺带重推
    await db.sessions.add({ ...session('s2'), syncState: 'synced', clientRev: 1 });

    const requeued = await requeueRejectedRows();

    expect(requeued).toBe(2);
    expect(await db.sessions.get('s1')).toMatchObject({ syncState: 'pending', clientRev: 4 });
    expect(await db.messages.get('m1')).toMatchObject({ syncState: 'pending', clientRev: 2 });
    expect((await db.sessions.get('s2'))?.syncState).toBe('synced');
  });

  it('没有可重试的行时返回 0，不改任何状态', async () => {
    await db.sessions.add({ ...session('s1'), syncState: 'synced' });

    expect(await requeueRejectedRows()).toBe(0);
    expect((await db.sessions.get('s1'))?.syncState).toBe('synced');
  });
});

describe('断网', () => {
  /** 用 stubGlobal 换掉 navigator.onLine（node 环境默认没有 navigator）。 */
  const setOnline = (online: boolean) => {
    vi.stubGlobal('navigator', { onLine: online });
  };

  it('断网时 flushPending 不发请求，本机行原样留着', async () => {
    setOnline(false);
    await db.sessions.add(session('s1'));

    const pending = await flushPending(500);

    expect(mocks.pushRows).not.toHaveBeenCalled();
    expect(pending).toBe(1);
    expect((await db.sessions.get('s1'))?.syncState).toBe('pending');
  });

  it('恢复在线后同一批行能被推上去', async () => {
    setOnline(false);
    await db.sessions.add(session('s1'));
    await flushPending(500);
    expect(mocks.pushRows).not.toHaveBeenCalled();

    setOnline(true);
    mocks.pushRows.mockResolvedValueOnce(accepted(1));
    await flushPending(2_000);

    expect(mocks.pushRows).toHaveBeenCalledTimes(1);
    expect((await db.sessions.get('s1'))?.syncState).toBe('synced');
  });

  it('没有 navigator（SSR/测试）时按在线处理，不能静默停摆', async () => {
    vi.stubGlobal('navigator', undefined);
    await db.sessions.add(session('s1'));
    mocks.pushRows.mockResolvedValueOnce(accepted(1));

    await flushPending(2_000);

    expect(mocks.pushRows).toHaveBeenCalledTimes(1);
  });
});

/**
 * P1：被判定 `stale` / `conversation_deleted` 的行再也不会被拉回来。
 *
 * `settleRow` 把它们标成 synced，原先的注释说「等下一轮 pull 用服务端版本覆盖」——但那条
 * 行的服务端 `version` 在本地编辑之前就已经越过游标，`version > since` 再也不会发它。
 * 所以这两类拒绝必须触发一次全量重拉（游标置空、从 0 拉）。
 */
describe('服务端拒绝后的全量重同步', () => {
  const seedCursor = async (userId: string): Promise<void> => {
    await db.syncMeta.put({ key: 'cursor', userId, value: 'v1:9', updatedAt: 1 });
    // 回填标记：用例只验重拉，不要顺带把整库标脏
    await db.syncMeta.put({ key: 'backfill', userId, value: '1', updatedAt: 1 });
  };

  it.each(['stale', 'conversation_deleted'])(
    '推送被判 %s 后丢弃游标并从 0 全量重拉',
    async (reason) => {
      vi.stubGlobal('navigator', { onLine: true });
      const userId = `u_${reason}`;
      await db.sessions.add({ ...session('s1'), syncState: 'pending', clientRev: 4, serverVersion: 2 });
      await seedCursor(userId);

      mocks.pushRows.mockResolvedValueOnce({
        version: 10,
        accepted: { conversations: 0, messages: 0 },
        rejected: [{ kind: 'conversation', id: 's1', reason }],
      });
      mocks.pullRows
        // 全量重拉的第一页
        .mockResolvedValueOnce({ cursor: 'v1:12', has_more: false, conversations: [], messages: [] })
        // 之后的长轮询挂住：别让用例在空转的 pump 里烧 CPU
        .mockImplementation(hangPullUntilReleased);

      startSync(userId);

      await waitFor(() => mocks.pullRows.mock.calls.length >= 1);
      // 关键：不是拿旧游标 'v1:9' 续拉，而是从 0 重拉
      expect(mocks.pullRows.mock.calls[0][0]).toBeNull();
      await waitForAsync(async () => (await db.syncMeta.get('cursor'))?.value === 'v1:12');
    },
  );

  it('stale 仍然计入「被覆盖的修改」计数（用户要看得见自己的改动没上去）', async () => {
    vi.stubGlobal('navigator', { onLine: true });
    await db.sessions.add({ ...session('s1'), syncState: 'pending', clientRev: 4, serverVersion: 2 });
    await seedCursor('u_superseded');

    mocks.pushRows.mockResolvedValueOnce({
      version: 10,
      accepted: { conversations: 0, messages: 0 },
      rejected: [{ kind: 'conversation', id: 's1', reason: 'stale' }],
    });
    mocks.pullRows.mockImplementation(hangPullUntilReleased);

    startSync('u_superseded');

    await waitFor(() => mocks.pushRows.mock.calls.length === 1);
    await waitFor(() => getStatus().superseded === 1);
  });
});

/**
 * P1：413 让推送永久停摆。
 *
 * 150 行 × 256KB 能组出 38.4MB 的请求体，超过服务端 32MB 上限 → 413；而 413 原被判成
 * fatal（不重试），重试又会重新组装出同一批。砍半是客户端唯一能推进的动作。
 */
describe('请求体超限（413）', () => {
  it('砍半重试一次后照常推上去，不停在原地', async () => {
    vi.stubGlobal('navigator', { onLine: true });
    await db.sessions.add({ ...session('s1'), syncState: 'pending' });
    mocks.pushRows.mockRejectedValueOnce(new SyncHttpError('请求体过大', 413, null));
    mocks.pushRows.mockResolvedValueOnce(accepted(2));

    await flushPending(2_000);

    expect(mocks.pushRows).toHaveBeenCalledTimes(2);
    expect((await db.sessions.get('s1'))?.syncState).toBe('synced');
  });

  it('413 不再被当成致命错误：即使砍半也失败，退避重试还会再来', async () => {
    vi.stubGlobal('navigator', { onLine: true });
    await db.sessions.add({ ...session('s1'), syncState: 'pending' });
    // 两次都是 413（砍半也没用）：行必须留在 pending 而不是被标成 error/synced
    mocks.pushRows.mockRejectedValue(new SyncHttpError('仍然过大', 413, null));

    await flushPending(2_000);

    expect((await db.sessions.get('s1'))?.syncState).toBe('pending');
  });
});

/** P1：`readPending` 无 limit 时，410 后整库标脏会把整个 IndexedDB 读进内存。 */
describe('待推队列读取上限', () => {
  it('库里有 400 条待推消息时，单批只组 150 条（读取按批 limit，不整库排序）', async () => {
    vi.stubGlobal('navigator', { onLine: true });
    await db.messages.bulkAdd(
      Array.from({ length: 400 }, (_, index) => message(`m${index}`, `sess_${index % 2}`)),
    );
    mocks.pushRows.mockResolvedValue(accepted(1, 0, 150));

    await flushPending(1_500);

    const first = mocks.pushRows.mock.calls[0][0] as { messages: unknown[] };
    expect(first.messages).toHaveLength(150);
  });
});

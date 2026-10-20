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

import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { MessageRecord, SessionRecord } from '../../types';
import { db } from '../../db';

const mocks = vi.hoisted(() => ({ pushRows: vi.fn(), pullRows: vi.fn() }));

vi.mock('./api', () => ({
  pushRows: mocks.pushRows,
  pullRows: mocks.pullRows,
  PULL_TIMEOUT_MS: 1_000,
  SyncHttpError: class SyncHttpError extends Error {
    status = 0;
    code: string | null = null;
    get isCursorReset(): boolean {
      return false;
    }
    get isFatal(): boolean {
      return true;
    }
  },
}));

const { flushPending, requeueRejectedRows } = await import('./engine');

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

beforeEach(async () => {
  mocks.pushRows.mockReset();
  mocks.pullRows.mockReset();
  await db.sessions.clear();
  await db.messages.clear();
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

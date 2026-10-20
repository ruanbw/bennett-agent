/**
 * 同步标记的行为锁（`frontend/src/db/index.ts` 里的表级 hook）。
 *
 * 为什么值得单独测：这套标记是「本地修改会不会被推送」的唯一开关。它坏掉的方式是
 * **静默的**——写入照常成功、界面照常工作，只有换设备时才发现某条消息没上去。
 *
 * 这里用 `fake-indexeddb` 在 node 环境里跑真实的 Dexie（vitest 没有 jsdom）：
 * hook 的语义（尤其是 updating 必须**返回**额外变更、不能改参数）只有真 Dexie 能验证，
 * mock 掉 db 的写法测不到这一层。
 */

import 'fake-indexeddb/auto';

import { beforeEach, describe, expect, it } from 'vitest';

import type { MessageRecord, SessionRecord } from '../types';
import { db, runSyncInternal } from './index';

const session = (id: string): SessionRecord => ({
  id,
  title: '标题',
  createdAt: 1,
  updatedAt: 1,
});

const message = (id: string): MessageRecord => ({
  id,
  sessionId: 'sess_1',
  role: 'user',
  content: '你好',
  createdAt: 1,
});

beforeEach(async () => {
  await db.sessions.clear();
  await db.messages.clear();
  await db.syncMeta.clear();
});

describe('同步标记 hook', () => {
  it('新建的行标记为 pending，修订号从 0 开始', async () => {
    await db.messages.add(message('m1'));

    const row = await db.messages.get('m1');
    expect(row?.syncState).toBe('pending');
    expect(row?.clientRev).toBe(0);
  });

  it('本地修改会重新标 pending 并把修订号 +1', async () => {
    await db.messages.add(message('m1'));
    await db.messages.update('m1', { content: '改了' });

    const row = await db.messages.get('m1');
    expect(row?.syncState).toBe('pending');
    expect(row?.clientRev).toBe(1);
    expect(row?.content).toBe('改了');
  });

  it('连续修改逐次递增（服务端靠它拒绝旧内容覆盖新内容）', async () => {
    await db.messages.add(message('m1'));
    await db.messages.update('m1', { content: 'a' });
    await db.messages.update('m1', { content: 'b' });
    await db.messages.update('m1', { content: 'c' });

    expect((await db.messages.get('m1'))?.clientRev).toBe(3);
  });

  it('会话表同样被标记（重命名/置顶/删除都要同步）', async () => {
    await db.sessions.add(session('s1'));
    await db.sessions.update('s1', { title: '新标题' });

    const row = await db.sessions.get('s1');
    expect(row?.syncState).toBe('pending');
    expect(row?.clientRev).toBe(1);
  });

  it('runSyncInternal 里的写入不触发标记（拉取与标记已同步走这条路）', async () => {
    await db.messages.add(message('m1'));

    await runSyncInternal(async () => {
      await db.messages.update('m1', { syncState: 'synced' });
      await db.messages.update('m1', { content: '服务端内容' });
    });

    const row = await db.messages.get('m1');
    expect(row?.syncState).toBe('synced');
    // 关键：内容变了但修订号不动 —— 否则每次拉取都会把自己标脏再推回去
    expect(row?.clientRev).toBe(0);
    expect(row?.content).toBe('服务端内容');
  });
});

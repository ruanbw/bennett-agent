/**
 * 本地缓存归属的行为锁。
 *
 * 这里出错的方式都很安静：照片里看不到、日志里也没有，只有「换账号后看到了别人的
 * 对话」或者「换账号后自己的对话全没了」。所以每条规则都要有用例盯着。
 */

import 'fake-indexeddb/auto';

import { beforeEach, describe, expect, it } from 'vitest';

import type { MessageRecord, SessionRecord } from '../types';
import { clearAllLocalData, db } from '../db';
import { LOCAL_OWNER_KEY, claimLocalData, readLocalOwner } from './local-owner';

const session = (id: string): SessionRecord => ({
  id,
  title: '上一个账号的会话',
  createdAt: 1,
  updatedAt: 1,
});

const message = (id: string): MessageRecord => ({
  id,
  sessionId: 's_old',
  role: 'user',
  content: '上一个账号的消息',
  createdAt: 1,
});

async function seedData(): Promise<void> {
  await db.sessions.put(session('s_old'));
  await db.messages.put(message('m_old'));
}

beforeEach(async () => {
  await db.sessions.clear();
  await db.messages.clear();
  await db.syncMeta.clear();
});

describe('claimLocalData', () => {
  it('全新安装：认领但不删任何东西', async () => {
    const result = await claimLocalData('user_a');

    expect(result).toEqual({ cleared: false, previousOwner: null });
    expect(await readLocalOwner()).toBe('user_a');
  });

  it('同一个账号（刷新 / 重新登录）保留历史', async () => {
    await claimLocalData('user_a');
    await seedData();

    const result = await claimLocalData('user_a');

    expect(result).toEqual({ cleared: false, previousOwner: 'user_a' });
    expect(await db.sessions.count()).toBe(1);
    expect(await db.messages.count()).toBe(1);
  });

  it('换账号：先清空会话与消息，再写新归属', async () => {
    await claimLocalData('user_a');
    await seedData();

    const result = await claimLocalData('user_b');

    expect(result).toEqual({ cleared: true, previousOwner: 'user_a' });
    expect(await db.sessions.count()).toBe(0);
    expect(await db.messages.count()).toBe(0);
    expect(await readLocalOwner()).toBe('user_b');
  });

  it('清空会连同步游标一起清掉：新账号不能从旧账号的游标开始拉', async () => {
    await claimLocalData('user_a');
    await db.syncMeta.put({ key: 'cursor', userId: 'user_a', value: 'c1', updatedAt: 1 });

    await claimLocalData('user_b');

    expect(await db.syncMeta.get('cursor')).toBeUndefined();
    expect(await db.syncMeta.get(LOCAL_OWNER_KEY)).toMatchObject({ userId: 'user_b' });
  });

  it('没有归属记录时用同步元数据推断：同一账号不清库', async () => {
    // 本模块上线前的老库：只有游标，没有 owner
    await seedData();
    await db.syncMeta.put({ key: 'cursor', userId: 'user_a', value: 'c1', updatedAt: 1 });

    const result = await claimLocalData('user_a');

    expect(result).toEqual({ cleared: false, previousOwner: 'user_a' });
    expect(await db.sessions.count()).toBe(1);
  });

  it('没有归属记录时用同步元数据推断：不同账号要清库（否则会推给对方）', async () => {
    await seedData();
    await db.syncMeta.put({ key: 'backfill', userId: 'user_a', value: '1', updatedAt: 1 });

    const result = await claimLocalData('user_b');

    expect(result).toEqual({ cleared: true, previousOwner: 'user_a' });
    expect(await db.sessions.count()).toBe(0);
  });

  it('显式归属优先于同步元数据', async () => {
    await claimLocalData('user_b');
    // 残留的旧游标（理论上会被清掉，这里模拟被别的路径写回）
    await db.syncMeta.put({ key: 'cursor', userId: 'user_a', value: 'c1', updatedAt: 1 });

    expect(await readLocalOwner()).toBe('user_b');
    expect((await claimLocalData('user_b')).cleared).toBe(false);
  });

  it('清空本地库会连归属一起清掉（登出后下一个账号从零开始）', async () => {
    await claimLocalData('user_a');

    await clearAllLocalData();

    expect(await readLocalOwner()).toBeNull();
  });

  it('并发认领按到达顺序串行执行，最终归属是最后一个', async () => {
    const [first, second] = await Promise.all([claimLocalData('user_a'), claimLocalData('user_b')]);

    // 串行保证：b 看到的是 a 写下的归属，因此知道自己需要清库
    expect(first).toEqual({ cleared: false, previousOwner: null });
    expect(second).toEqual({ cleared: true, previousOwner: 'user_a' });
    expect(await readLocalOwner()).toBe('user_b');
  });
});

describe('无归属老库（L-07）', () => {
  it('没有归属记录但已有会话/消息：先清空再认领，避免下一个账号看到', async () => {
    await seedData(); // 老库：有数据，但没有 owner / cursor / backfill

    const result = await claimLocalData('user_b');

    expect(result.cleared).toBe(true);
    expect(await db.sessions.count()).toBe(0);
    expect(await db.messages.count()).toBe(0);
    expect(await readLocalOwner()).toBe('user_b');
  });

  it('没有归属记录且本地为空：认领但不标记 cleared', async () => {
    const result = await claimLocalData('user_a');

    expect(result).toEqual({ cleared: false, previousOwner: null });
  });
});

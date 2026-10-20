/**
 * 本机数据统计与字节格式化的行为锁。
 *
 * 设置面板里的数字必须和侧边栏列表对得上：墓碑会话（同步用的删除标记）不能被算进去，
 * 否则用户会看到「设置里 4 个、侧边栏 3 个」这种无法解释的差异。
 */

import 'fake-indexeddb/auto';

import { beforeEach, describe, expect, it } from 'vitest';

import type { MessageRecord, SessionRecord } from '../types';
import { db } from '../db';
import { formatBytes, readLocalDataStats } from './local-stats';

const session = (id: string, deleted = false): SessionRecord => ({
  id,
  title: '会话',
  createdAt: 1,
  updatedAt: 1,
  ...(deleted ? { deleted: true } : {}),
});

const message = (id: string, sessionId: string): MessageRecord => ({
  id,
  sessionId,
  role: 'user',
  content: '你好',
  createdAt: 1,
});

beforeEach(async () => {
  await db.sessions.clear();
  await db.messages.clear();
  await db.syncMeta.clear();
});

describe('formatBytes', () => {
  it('小于 1KB 显示字节', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1023)).toBe('1023 B');
  });

  it('按 1024 进制换单位，保留一位小数', () => {
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(1.25 * 1024 * 1024)).toBe('1.3 MB');
    expect(formatBytes(3 * 1024 * 1024 * 1024)).toBe('3.0 GB');
  });

  it('未知/非法值显示「未知」而不是 0 B（0 B 会被理解成没占空间）', () => {
    expect(formatBytes(null)).toBe('未知');
    expect(formatBytes(Number.NaN)).toBe('未知');
    expect(formatBytes(-1)).toBe('未知');
  });
});

describe('readLocalDataStats', () => {
  it('会话排除墓碑，消息计数与库一致', async () => {
    await db.sessions.bulkPut([session('s1'), session('s2'), session('s3', true)]);
    await db.messages.bulkPut([
      message('m1', 's1'),
      message('m2', 's1'),
      message('m3', 's2'),
    ]);

    const stats = await readLocalDataStats();

    expect(stats.sessions).toBe(2);
    expect(stats.messages).toBe(3);
  });

  it('没有 storage.estimate 的环境里占用为 null（不编造数字）', async () => {
    const stats = await readLocalDataStats();
    expect(stats.usageBytes).toBeNull();
  });

  it('空库返回 0 而不是抛错', async () => {
    const stats = await readLocalDataStats();
    expect(stats.sessions).toBe(0);
    expect(stats.messages).toBe(0);
  });
});

import { describe, expect, it } from 'vitest';

import type { SyncStatus } from './engine';
import {
  describePendingCount,
  describeRejectionReason,
  describeSyncStatus,
  formatRelativeTime,
  isRejectionRetryable,
} from './status-view';

const NOW = Date.UTC(2026, 9, 21, 12, 0, 0);

const status = (patch: Partial<SyncStatus> = {}): SyncStatus => ({
  running: true,
  pending: 0,
  rejected: 0,
  rejectedReason: null,
  syncing: false,
  lastError: null,
  lastSyncedAt: NOW - 10_000,
  ...patch,
});

describe('同步状态优先级', () => {
  it('错误优先于待推送与被拒：先说「失败了」，否则用户会以为只是慢', () => {
    const view = describeSyncStatus(
      status({ lastError: '网络中断', pending: 3, rejected: 1, rejectedReason: 'too_large' }),
    );

    expect(view.tone).toBe('error');
    expect(view.headline).toBe('同步失败');
    expect(view.detail).toContain('网络中断');
    expect(view.canRetry).toBe(true);
    // 角标要覆盖「全部只在本机」的行，不能只数 pending
    expect(view.badgeCount).toBe(4);
  });

  it('被拒优先于待推送：被拒的行不会自己好，不能让「还在排队」盖住它', () => {
    const view = describeSyncStatus(
      status({ pending: 2, rejected: 1, rejectedReason: 'quota_exceeded' }),
    );

    expect(view.tone).toBe('warning');
    expect(view.headline).toBe('1 条内容无法同步');
    expect(view.detail).toContain('服务器存储配额已满');
    expect(view.canRetry).toBe(true);
  });

  it('推送在飞时说「正在上传」，而不是「等待上传」', () => {
    const uploading = describeSyncStatus(status({ pending: 2, syncing: true }));
    const waiting = describeSyncStatus(status({ pending: 2, syncing: false }));

    expect(uploading.tone).toBe('busy');
    expect(uploading.headline).toBe('正在上传变更…');
    expect(waiting.headline).toBe('2 条变更等待上传');
    // 自动重试已经在路上：不给用户一个没用的「重试」按钮
    expect(waiting.canRetry).toBe(false);
  });

  it('首次同步尚未完成时是进行中，不是「已同步」', () => {
    const view = describeSyncStatus(status({ lastSyncedAt: null }));

    expect(view.tone).toBe('busy');
    expect(view.headline).toBe('正在建立同步…');
  });

  it('一切正常时给出可确认的结论', () => {
    const view = describeSyncStatus(status({ lastSyncedAt: NOW - 3 * 60_000 }));

    expect(view.tone).toBe('ok');
    expect(view.headline).toBe('已同步');
    expect(view.detail).toContain('已保存到服务器');
    expect(view.badgeCount).toBe(0);
  });

  it('引擎没在跑时明说未启用，而不是假装已同步', () => {
    const view = describeSyncStatus(status({ running: false, lastSyncedAt: null }));

    expect(view.tone).toBe('idle');
    expect(view.headline).toBe('同步未启用');
  });
});

describe('相对时间', () => {
  it('按尺度切换单位', () => {
    expect(formatRelativeTime(NOW - 1_000, NOW)).toBe('刚刚');
    expect(formatRelativeTime(NOW - 12_000, NOW)).toBe('12 秒前');
    expect(formatRelativeTime(NOW - 5 * 60_000, NOW)).toBe('5 分钟前');
    expect(formatRelativeTime(NOW - 3 * 3_600_000, NOW)).toBe('3 小时前');
    expect(formatRelativeTime(NOW - 2 * 86_400_000, NOW)).toBe('2 天前');
  });

  it('本机时钟快于服务器时按「刚刚」处理，不显示负数', () => {
    expect(formatRelativeTime(NOW + 60_000, NOW)).toBe('刚刚');
  });

  it('超过 30 天改用日期：那个尺度上「N 天前」帮不上判断', () => {
    const at = NOW - 60 * 86_400_000;
    expect(formatRelativeTime(at, NOW)).toBe(new Date(at).toLocaleDateString('zh-CN'));
  });

  it('从未同步过时说「从未」', () => {
    expect(formatRelativeTime(null, NOW)).toBe('从未');
  });
});

describe('拒收原因文案', () => {
  it('已知原因翻成人话', () => {
    expect(describeRejectionReason('too_large')).toBe('单条内容超过服务器上限');
    expect(describeRejectionReason('conversation_missing')).toBe('所属会话在服务器上已不存在');
  });

  it('未知原因保留机器码，但先给一句人话', () => {
    expect(describeRejectionReason('some_new_code')).toContain('服务器拒绝了这些内容');
    expect(describeRejectionReason('some_new_code')).toContain('some_new_code');
  });

  it('没有原因时也说得通', () => {
    expect(describeRejectionReason(null)).toBe('服务器拒绝了这些内容');
  });
});

describe('被拒内容能否重试', () => {
  it('单条超过上限不可重试：数据本身放不进去，重试只是让用户反复失望', () => {
    expect(isRejectionRetryable('too_large')).toBe(false);
  });

  it('配额满 / 会话不存在 / 未知原因都允许重新上传', () => {
    expect(isRejectionRetryable('quota_exceeded')).toBe(true);
    expect(isRejectionRetryable('conversation_missing')).toBe(true);
    expect(isRejectionRetryable(null)).toBe(true);
  });
});

describe('待上传数量文案', () => {
  it('有排队就说条数', () => {
    expect(describePendingCount(3, false)).toBe('3 条');
    expect(describePendingCount(3, true)).toBe('3 条');
  });

  it('没有排队但正在检查时不说「无」（会与「正在上传变更…」自相矛盾）', () => {
    expect(describePendingCount(0, true)).toBe('正在检查…');
    expect(describePendingCount(0, false)).toBe('无');
  });
});

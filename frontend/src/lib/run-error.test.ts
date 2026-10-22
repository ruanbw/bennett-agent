import { describe, expect, it } from 'vitest';

import { describeCompactionNotice, describeErrorHint, describeErrorLabel, readKnownErrorCode } from './run-error';

describe('readKnownErrorCode', () => {
  it('认三种服务端分类', () => {
    expect(readKnownErrorCode('context_overflow')).toBe('context_overflow');
    expect(readKnownErrorCode('model_unavailable')).toBe('model_unavailable');
    expect(readKnownErrorCode('internal_error')).toBe('internal_error');
  });

  it('协议外的取值与非字符串一律当未分类（不能把未知字符串带进 run）', () => {
    expect(readKnownErrorCode('something_new')).toBeNull();
    expect(readKnownErrorCode(undefined)).toBeNull();
    expect(readKnownErrorCode(null)).toBeNull();
    expect(readKnownErrorCode(42)).toBeNull();
  });
});

describe('describeErrorLabel', () => {
  it('按分类换标题：上下文已满不是「错误」，模型不可用也不是', () => {
    expect(describeErrorLabel('context_overflow')).toBe('上下文已满');
    expect(describeErrorLabel('model_unavailable')).toBe('模型不可用');
    expect(describeErrorLabel('internal_error')).toBe('服务端错误');
    expect(describeErrorLabel(null)).toBe('错误');
  });
});

describe('describeErrorHint', () => {
  it('模型不可用给出确定动作：换模型 + 刷新列表', () => {
    const hint = describeErrorHint('model_unavailable');
    expect(hint).toContain('换一个模型');
    expect(hint).toContain('刷新');
  });

  it('上下文已满说明重试无用并给出出路', () => {
    const hint = describeErrorHint('context_overflow');
    expect(hint).toContain('重试不会成功');
    expect(hint).toContain('新建会话');
  });

  it('未分类与服务端错误不给凭空的建议', () => {
    expect(describeErrorHint(null)).toBe('');
    expect(describeErrorHint('internal_error')).toBe('');
  });
});

describe('describeCompactionNotice', () => {
  const base = {
    strategy: 'summarize' as const,
    droppedMessages: 6,
    truncatedMessages: 2,
    usedTokensBefore: 120_000,
    usedTokensAfter: 30_000,
    limitTokens: 40_000,
  };

  it('折叠早期轮次时说明丢了多少、省了多少', () => {
    const text = describeCompactionNotice(base);

    expect(text).toContain('折叠');
    expect(text).toContain('6');
    expect(text).toContain('90k');
  });

  it('必须说明「早期细节可能不再完整」这一代价', () => {
    // 不说，用户会把「模型忘了刚才说的」当成模型变笨，而现场没有任何痕迹指向压缩
    expect(describeCompactionNotice(base)).toContain('早期细节可能不再完整');
  });

  it('只截断时说明截断条数，不提摘要', () => {
    const text = describeCompactionNotice({
      ...base,
      strategy: 'truncate',
      droppedMessages: 0,
      truncatedMessages: 3,
    });

    expect(text).toContain('截断');
    expect(text).toContain('3');
    expect(text).not.toContain('摘要');
  });

  it('同时发生截断与折叠时两条都要说', () => {
    const text = describeCompactionNotice(base);

    expect(text).toContain('另有 2 条超长消息被截断');
  });

  it('省出的量不为负：摘要比原文还长时不能写「省出 -5k」', () => {
    const text = describeCompactionNotice({
      ...base,
      usedTokensBefore: 100,
      usedTokensAfter: 200,
    });

    expect(text).toContain('省出约 0 tokens');
    expect(text).not.toContain('-');
  });

  it('数字口径与上下文用量指示器一致（k/M，而不是千分位）', () => {
    // 同一份数据在两处显示成「12k」与「12,000」会让人以为是两个不同的量
    // 12,000 - 1,000 = 11,000 → "11k"（不是 "11,000"）
    const text = describeCompactionNotice({ ...base, usedTokensBefore: 12_000, usedTokensAfter: 1_000 });
    expect(text).toContain('11k');
    expect(text).not.toContain('11,000');
  });
});

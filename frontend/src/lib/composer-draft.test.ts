import { describe, expect, it, vi } from 'vitest';

import {
  COMPOSER_DRAFT_KEY,
  MAX_DRAFT_CHARS,
  clearComposerDrafts,
  MAX_DRAFTS,
  readDraft,
  readDrafts,
  writeDraft,
} from './composer-draft';

/** 内存版 localStorage；可注入抛错。 */
function makeStorage(initial: Record<string, string> = {}, options: { failWrites?: boolean } = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      if (options.failWrites) throw new Error('QuotaExceededError');
      data.set(key, value);
    },
    removeItem: (key: string) => {
      data.delete(key);
    },
  };
}

describe('输入框草稿', () => {
  it('写入后能按会话读回来，不同会话互不干扰', () => {
    const storage = makeStorage();

    writeDraft(storage, 's1', '第一句话', 1_000);
    writeDraft(storage, 's2', '第二句话', 2_000);

    expect(readDraft(storage, 's1')).toBe('第一句话');
    expect(readDraft(storage, 's2')).toBe('第二句话');
    expect(readDraft(storage, 's3')).toBe('');
  });

  it('清空内容等于删除草稿（发送成功后输入框会为空）', () => {
    const storage = makeStorage();
    writeDraft(storage, 's1', '草稿', 1_000);

    writeDraft(storage, 's1', '   ', 2_000);

    expect(readDraft(storage, 's1')).toBe('');
    expect(readDrafts(storage)).toEqual({});
  });

  it('超长粘贴不存，并清掉旧条目（不能把配额吃满）', () => {
    const storage = makeStorage();
    writeDraft(storage, 's1', '短草稿', 1_000);

    writeDraft(storage, 's1', 'x'.repeat(MAX_DRAFT_CHARS + 1), 2_000);

    expect(readDraft(storage, 's1')).toBe('');
  });

  it('只保留最近的 MAX_DRAFTS 个会话', () => {
    const storage = makeStorage();

    for (let index = 0; index < MAX_DRAFTS + 5; index += 1) {
      writeDraft(storage, `s${index}`, `内容${index}`, 1_000 + index);
    }

    const drafts = readDrafts(storage);
    expect(Object.keys(drafts)).toHaveLength(MAX_DRAFTS);
    // 最早的五条被淘汰
    expect(drafts.s0).toBeUndefined();
    expect(drafts[`s${MAX_DRAFTS + 4}`]).toEqual({ text: `内容${MAX_DRAFTS + 4}`, at: 1_000 + MAX_DRAFTS + 4 });
  });

  it('存储里是坏 JSON / 脏数据时当作没有草稿，不抛异常', () => {
    const broken = makeStorage({ [COMPOSER_DRAFT_KEY]: '{not json' });
    expect(readDrafts(broken)).toEqual({});

    const dirty = makeStorage({
      [COMPOSER_DRAFT_KEY]: JSON.stringify({ s1: { text: 42 }, s2: { text: 'ok' }, '': { text: 'x' } }),
    });
    expect(readDrafts(dirty)).toEqual({ s2: { text: 'ok', at: 0 } });
  });

  it('读/写抛异常时静默降级（隐私模式下输入框仍然要能用）', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const failingRead = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {},
    };
    expect(readDrafts(failingRead)).toEqual({});

    const failingWrite = makeStorage({}, { failWrites: true });
    expect(() => writeDraft(failingWrite, 's1', '内容')).not.toThrow();
  });
});

describe('clearComposerDrafts（L-06）', () => {
  it('登出后草稿不再可读', () => {
    const storage = makeStorage();
    writeDraft(storage, 's1', '没发送的敏感文本', 1_000);

    clearComposerDrafts(storage);

    expect(readDraft(storage, 's1')).toBe('');
    expect(storage.data.has(COMPOSER_DRAFT_KEY)).toBe(false);
  });

  it('存储不支持 removeItem 时退化为写空对象，不抛错', () => {
    const storage = makeStorage({ [COMPOSER_DRAFT_KEY]: JSON.stringify({ s1: { text: 'x', at: 1 } }) });

    clearComposerDrafts(storage);

    expect(readDrafts(storage)).toEqual({});
  });

  it('存储访问抛错时不影响登出流程', () => {
    const storage = {
      getItem: () => null,
      setItem: () => {
        throw new Error('SecurityError');
      },
    };

    expect(() => clearComposerDrafts(storage)).not.toThrow();
  });
});

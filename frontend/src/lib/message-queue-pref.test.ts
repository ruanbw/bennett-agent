import { describe, expect, it } from 'vitest';

import {
  DEFAULT_MESSAGE_QUEUE_PREF,
  MESSAGE_QUEUE_PREF_STORAGE_KEY,
  messageQueuePrefFromStorageEvent,
  normalizeMessageQueuePref,
  readMessageQueuePref,
  writeMessageQueuePref,
  type MessageQueuePrefStorage,
} from './message-queue-pref';

/** 按 key/value 记录的假 localStorage；可注入抛异常行为。 */
function fakeStorage(
  init: Record<string, string> = {},
  opts: { throwOnGet?: boolean; throwOnSet?: boolean } = {}
): MessageQueuePrefStorage & { data: Map<string, string> } {
  const data = new Map(Object.entries(init));
  return {
    data,
    getItem(key) {
      if (opts.throwOnGet) throw new Error('storage unavailable');
      return data.get(key) ?? null;
    },
    setItem(key, value) {
      if (opts.throwOnSet) throw new Error('quota exceeded');
      data.set(key, value);
    },
  };
}

describe('normalizeMessageQueuePref', () => {
  it('认识两档合法值', () => {
    expect(normalizeMessageQueuePref('insert')).toBe('insert');
    expect(normalizeMessageQueuePref('block')).toBe('block');
  });

  it('未知值、非字符串、空值都回落默认（block）', () => {
    expect(normalizeMessageQueuePref('queue')).toBe(DEFAULT_MESSAGE_QUEUE_PREF);
    expect(normalizeMessageQueuePref(42)).toBe(DEFAULT_MESSAGE_QUEUE_PREF);
    expect(normalizeMessageQueuePref(null)).toBe(DEFAULT_MESSAGE_QUEUE_PREF);
  });
});

describe('readMessageQueuePref / writeMessageQueuePref', () => {
  it('没存过时读出默认 block', () => {
    expect(readMessageQueuePref(fakeStorage())).toBe('block');
  });

  it('写 insert 后读回 insert；写回 block 亦然', () => {
    const storage = fakeStorage();
    writeMessageQueuePref('insert', storage);
    expect(storage.data.get(MESSAGE_QUEUE_PREF_STORAGE_KEY)).toBe('insert');
    expect(readMessageQueuePref(storage)).toBe('insert');

    writeMessageQueuePref('block', storage);
    expect(readMessageQueuePref(storage)).toBe('block');
  });

  it('读到脏值回落默认而不是把界面停在两档之外', () => {
    expect(readMessageQueuePref(fakeStorage({ [MESSAGE_QUEUE_PREF_STORAGE_KEY]: 'bogus' }))).toBe(
      'block'
    );
  });

  it('存储不可用（null）时读默认、写静默', () => {
    expect(readMessageQueuePref(null)).toBe('block');
    expect(() => writeMessageQueuePref('insert', null)).not.toThrow();
  });

  it('读/写抛异常时回落默认、只记日志，不往上抛', () => {
    expect(readMessageQueuePref(fakeStorage({}, { throwOnGet: true }))).toBe('block');
    expect(() => writeMessageQueuePref('insert', fakeStorage({}, { throwOnSet: true }))).not.toThrow();
  });
});

describe('messageQueuePrefFromStorageEvent', () => {
  it('本键的新值被白名单化后返回', () => {
    expect(
      messageQueuePrefFromStorageEvent({ key: MESSAGE_QUEUE_PREF_STORAGE_KEY, newValue: 'insert' })
    ).toBe('insert');
  });

  it('别的键返回 null（调用方据此忽略）', () => {
    expect(messageQueuePrefFromStorageEvent({ key: 'unrelated', newValue: 'insert' })).toBeNull();
  });

  it('key 为 null（storage.clear）时回落默认', () => {
    expect(messageQueuePrefFromStorageEvent({ key: null, newValue: null })).toBe('block');
  });
});

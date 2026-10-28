import { describe, expect, it } from 'vitest';

import {
  dragHasFiles,
  foreignFileDropMessage,
  isInsideComposerRegion,
  shouldBlockWindowFileDrop,
} from './file-drop';

/** 只实现 closest 的假目标：node 环境没有 DOM。 */
function fakeTarget(matches: boolean): EventTarget {
  return { closest: () => (matches ? ({} as Element) : null) } as unknown as EventTarget;
}

describe('dragHasFiles', () => {
  it('只认 Files，文本/链接拖放不拦（会破坏输入框内拖动文字）', () => {
    expect(dragHasFiles(['Files'])).toBe(true);
    expect(dragHasFiles(['text/plain', 'Files'])).toBe(true);
    expect(dragHasFiles(['text/plain'])).toBe(false);
    expect(dragHasFiles([])).toBe(false);
    expect(dragHasFiles(undefined)).toBe(false);
    expect(dragHasFiles(null)).toBe(false);
  });
});

describe('isInsideComposerRegion', () => {
  it('输入框内由 Composer 自己处理，窗口兜底不重复提示', () => {
    expect(isInsideComposerRegion(fakeTarget(true))).toBe(true);
    expect(isInsideComposerRegion(fakeTarget(false))).toBe(false);
  });

  it('非元素目标（window / document）当作框外', () => {
    expect(isInsideComposerRegion(null)).toBe(false);
    expect(isInsideComposerRegion({} as EventTarget)).toBe(false);
  });
});

describe('shouldBlockWindowFileDrop', () => {
  it('拖文件到界面其它地方必须拦下（否则浏览器导航到文件、应用状态全丢）', () => {
    expect(
      shouldBlockWindowFileDrop({ defaultPrevented: false, types: ['Files'], target: fakeTarget(false) }),
    ).toBe(true);
  });

  it('框内不拦：Composer 已处理并给了更具体的提示', () => {
    expect(
      shouldBlockWindowFileDrop({ defaultPrevented: false, types: ['Files'], target: fakeTarget(true) }),
    ).toBe(false);
  });

  it('已被别人 preventDefault 的事件不重复处理', () => {
    expect(
      shouldBlockWindowFileDrop({ defaultPrevented: true, types: ['Files'], target: fakeTarget(false) }),
    ).toBe(false);
  });

  it('非文件拖放不拦（输入框内拖动文字、拖链接）', () => {
    expect(
      shouldBlockWindowFileDrop({ defaultPrevented: false, types: ['text/plain'], target: fakeTarget(false) }),
    ).toBe(false);
  });
});

describe('foreignFileDropMessage', () => {
  it('说清「没加上」，并且把原因说成「暂未开放」而不是「后端只接受文本」', () => {
    const one = foreignFileDropMessage(1);
    expect(one).toContain('没有添加');
    expect(one).toContain('暂未开放');
    // 「后端只接受文本」会让用户以为这是部署缺陷、去催运维；
    // 真实情况是这个产品还没开放该功能。
    expect(one).not.toContain('后端');

    expect(foreignFileDropMessage(3)).toContain('3 个文件');
  });
});

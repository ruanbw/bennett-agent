import { describe, expect, it } from 'vitest';

import { resolveDrawerRestoreTarget, shouldRestoreFocus, trapTabIndex } from './focus-trap';

describe('trapTabIndex', () => {
  it('中间元素正常前后移动', () => {
    expect(trapTabIndex(3, 0, false)).toBe(1);
    expect(trapTabIndex(3, 2, true)).toBe(1);
  });

  it('两端绕回形成环，不会跑出抽屉', () => {
    expect(trapTabIndex(3, 2, false)).toBe(0);
    expect(trapTabIndex(3, 0, true)).toBe(2);
  });

  it('焦点不在列表里时从头/从尾进入', () => {
    expect(trapTabIndex(3, -1, false)).toBe(0);
    expect(trapTabIndex(3, -1, true)).toBe(2);
  });

  it('没有可聚焦元素时返回 null（调用方据此不拦截按键）', () => {
    expect(trapTabIndex(0, 0, false)).toBeNull();
    expect(trapTabIndex(0, -1, true)).toBeNull();
  });

  it('只有一个元素时 Tab 原地不动', () => {
    expect(trapTabIndex(1, 0, false)).toBe(0);
    expect(trapTabIndex(1, 0, true)).toBe(0);
  });
});

describe('shouldRestoreFocus', () => {
  const fakeElement = (connected = true, insideDrawer = false) => {
    const element = {
      isConnected: connected,
      closest: () => (insideDrawer ? {} : null),
    } as unknown as Element;
    const drawer = {
      contains: (node: Node) => node === element && insideDrawer,
    } as unknown as Element;
    return { element, drawer };
  };

  it('打开前的焦点在抽屉外时，关闭后要还回去', () => {
    const { element, drawer } = fakeElement(true, false);
    expect(shouldRestoreFocus(element, drawer)).toBe(element);
  });

  it('焦点本来就在抽屉里时不抢', () => {
    const { element, drawer } = fakeElement(true, true);
    expect(shouldRestoreFocus(element, drawer)).toBeNull();
  });

  it('没有记录、或节点已经卸载时返回 null', () => {
    const { drawer } = fakeElement(true, false);
    expect(shouldRestoreFocus(null, drawer)).toBeNull();
    const gone = { isConnected: false } as unknown as Element;
    expect(shouldRestoreFocus(gone, drawer)).toBeNull();
  });

  it('activeElement 是 <body>（点击按钮没给焦点）时不还原', () => {
    const doc = { body: null as unknown };
    const body = { isConnected: true, ownerDocument: doc } as unknown as Element;
    doc.body = body;
    expect(shouldRestoreFocus(body, null)).toBeNull();
  });
});

describe('resolveDrawerRestoreTarget', () => {
  /** node 环境没有 DOM：用带 focus() 的最小替身，够验证「能不能聚焦」的分支。 */
  const node = (overrides: { isConnected?: boolean; focusable?: boolean } = {}) =>
    ({
      isConnected: overrides.isConnected ?? true,
      focus: overrides.focusable === false ? undefined : () => {},
    }) as unknown as Element;

  it('打开前的焦点还活着就还给它', () => {
    const recorded = node();
    const fallback = node();
    expect(resolveDrawerRestoreTarget(recorded, null, fallback)).toBe(recorded);
  });

  it('原按钮已随抽屉展开被卸载时，退回「重新打开抽屉」的入口', () => {
    const fallback = node();
    expect(resolveDrawerRestoreTarget(node({ isConnected: false }), null, fallback)).toBe(fallback);
  });

  it('焦点本来就在抽屉里、且没有兜底时不抢焦点', () => {
    const inside = node();
    const drawer = { contains: (other: unknown) => other === inside, isConnected: true } as unknown as Element;
    expect(resolveDrawerRestoreTarget(inside, drawer, null)).toBeNull();
  });

  it('兜底节点也不可聚焦时返回 null（交给浏览器默认行为）', () => {
    expect(resolveDrawerRestoreTarget(null, null, node({ focusable: false }))).toBeNull();
  });
});

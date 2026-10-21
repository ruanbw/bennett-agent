/**
 * 焦点陷阱的纯逻辑：把 Tab / Shift+Tab 限制在一组元素里。
 *
 * 移动端抽屉打开时背景已经 `inert`，但键盘的 Tab 仍可能跑到浏览器 UI 或地址栏之后
 * 回到页面顶部；抽屉作为「临时覆盖层」，焦点必须在它内部循环。判定抽成纯函数，
 * 这样环形绕回的边界可以在 vitest 里直接锁住（组件层没有 jsdom）。
 */

/**
 * 计算下一个该聚焦的下标。
 *
 * @param count 可聚焦元素个数（0 表示没有可聚焦元素，返回 null）
 * @param activeIndex 当前焦点在列表里的下标；-1 表示焦点不在列表内
 * @param backwards true = Shift+Tab
 */
export function trapTabIndex(
  count: number,
  activeIndex: number,
  backwards: boolean,
): number | null {
  if (count <= 0) return null;
  if (activeIndex < 0) {
    // 焦点不在列表里：向前从最后一个开始，向后从第一个开始
    return backwards ? count - 1 : 0;
  }
  const next = backwards ? activeIndex - 1 : activeIndex + 1;
  // 两端绕回，形成环
  if (next < 0) return count - 1;
  if (next >= count) return 0;
  return next;
}

/**
 * 抽屉打开前焦点在哪里、关闭后要不要还回去。
 *
 * 只在「焦点原本在抽屉外」时才记录：用户可能本来就在侧边栏里操作（例如点了搜索框），
 * 关闭后把焦点抢到别的按钮上反而更差。返回 null 表示关闭时不需要恢复焦点。
 */
export function shouldRestoreFocus(
  previouslyFocused: Element | null,
  drawer: Element | null,
): Element | null {
  if (!previouslyFocused) return null;
  if (drawer && drawer.contains(previouslyFocused)) return null;
  // 已经不在文档里的节点（被卸载的按钮）不能再聚焦
  if (!previouslyFocused.isConnected) return null;
  // `<body>` / `<html>` 不是「用户刚才操作的位置」：桌面端点击按钮不一定给它焦点
  // （Safari 就不会），此时 activeElement 是 body。还焦点给 body 等于不还原。
  const doc = previouslyFocused.ownerDocument;
  if (doc && (previouslyFocused === doc.body || previouslyFocused === doc.documentElement)) {
    return null;
  }
  return previouslyFocused;
}

/**
 * 抽屉关闭后到底该把焦点放哪。
 *
 * 打开抽屉的那个按钮通常就在抽屉外面，而「展开侧边栏」按钮会随着抽屉展开被卸载——
 * 关闭时它已不在文档里（`shouldRestoreFocus` 返回 null），焦点于是掉到 `<body>` 上，
 * 键盘用户得从头 Tab 一遍。所以退一步找回「重新打开抽屉」的入口按钮。
 *
 * @param recorded 打开前记录的焦点元素
 * @param drawer 抽屉节点（关闭时为 null）
 * @param fallback 兜底：重新打开抽屉的按钮
 */
export function resolveDrawerRestoreTarget(
  recorded: Element | null,
  drawer: Element | null,
  fallback: Element | null,
): Element | null {
  const restored = shouldRestoreFocus(recorded, drawer);
  if (canFocus(restored)) return restored;
  if (canFocus(fallback)) return fallback;
  return null;
}

/** 能不能真的聚焦：需要 `focus()` 方法且仍在文档里（不依赖 `instanceof HTMLElement`，便于纯逻辑测试）。 */
function canFocus(element: Element | null): boolean {
  if (!element) return false;
  if (element.isConnected === false) return false;
  return typeof (element as { focus?: unknown }).focus === 'function';
}

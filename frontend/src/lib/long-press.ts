/**
 * 长按手势的判定参数与纯逻辑。
 *
 * 触屏上没有 hover：会话行右侧的「…」按钮只有 24px，手指很难点准。除了把触屏点击区域
 * 放大，还补一条「长按整行等同于打开操作菜单」，与移动端列表的通用习惯一致。
 *
 * 手势的难点全在边界上，所以判定抽成纯函数：
 * - 长按到时阈值才算数（默认 500ms，短于这个数更像误触）；
 * - 手指在阈值时间内移动超过容差就取消（用户在滑动列表，不是要开菜单）；
 * - 长按触发后**必须吞掉随后那次 click**，否则开菜单的同时还会切走会话。
 */

/** 触发长按的时长（毫秒）。 */
export const LONG_PRESS_MS = 500;

/** 手指允许的抖动范围（像素）：超过就当作滚动/滑动而不是长按。 */
export const LONG_PRESS_MOVE_TOLERANCE_PX = 10;

export interface Point {
  x: number;
  y: number;
}

/**
 * 手指是否已经移动出长按容差。
 *
 * 用欧氏距离而不是单看某一轴：斜向滑动同样应该取消，否则斜着滑列表会突然弹出菜单。
 */
export function movedBeyondLongPressTolerance(
  start: Point,
  current: Point,
  tolerance: number = LONG_PRESS_MOVE_TOLERANCE_PX,
): boolean {
  const dx = current.x - start.x;
  const dy = current.y - start.y;
  return Math.hypot(dx, dy) > tolerance;
}

/**
 * 这个指针类型是否该参与长按。
 *
 * 只认 `touch`：鼠标用户有右键与「…」按钮，手写笔（`pen`）精度足够点小按钮，
 * 给它们加长按只会让「按住拖选标题」这类操作意外弹出菜单。
 */
export function isLongPressPointer(pointerType: string): boolean {
  return pointerType === 'touch';
}

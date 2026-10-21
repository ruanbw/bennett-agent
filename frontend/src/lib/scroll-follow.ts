/**
 * 滚动容器的「是否贴底」判断。
 *
 * 直播内容（思考过程、流式回答）需要自动吸底，但用户一旦往上翻，就不该在每个增量
 * 到达时把他拽回底部——那等于不让人读已经生成的内容。判断留成纯函数，避免在
 * 组件里写一份只有浏览器才能验证的减法。
 */

export interface ScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** 距底部多少像素以内仍算「贴底」：留一点容差，避免亚像素与滚动惯性反复切换。 */
export const FOLLOW_THRESHOLD_PX = 24;

export function isNearScrollBottom(
  metrics: ScrollMetrics,
  threshold: number = FOLLOW_THRESHOLD_PX,
): boolean {
  // 内容不满一屏时不存在「往上翻」，始终视为贴底
  const distance = metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight;
  return distance <= threshold;
}

/**
 * 「按 Esc 停止生成」的判定与停止后的焦点落点。
 *
 * 停止按钮在生成中替换发送按钮、生成结束又被换回去：用户点完（或用键盘按完）停止，
 * 焦点所在的那个按钮已经不在 DOM 里，浏览器把焦点退回 `<body>`，键盘用户得从头 Tab
 * 一遍。停止后应把焦点交回输入框——这也是他接下来最可能操作的地方。
 *
 * 判定单独抽出来是因为 Esc 在应用里还有别的含义（关搜索、关菜单、关弹层），什么情况下
 * 才轮到「停止生成」需要能被单测锁住。
 */

export interface StopOnEscapeInput {
  /** 当前是否正在生成。 */
  isRunning: boolean;
  /** 按键是否发生在输入法组合中：此时 Esc 是取消候选词。 */
  isComposing?: boolean;
  /** 有弹层（设置 / 确认框 / 菜单）打开时 Esc 属于那个弹层。 */
  hasOpenOverlay?: boolean;
  /** 别的处理器已经消费了这个按键。 */
  defaultPrevented?: boolean;
  /** 焦点在确认框之类的模态里。 */
  inModal?: boolean;
}

export function shouldStopOnEscape({
  isRunning,
  isComposing = false,
  hasOpenOverlay = false,
  defaultPrevented = false,
  inModal = false,
}: StopOnEscapeInput): boolean {
  if (!isRunning) return false;
  if (isComposing || defaultPrevented) return false;
  if (hasOpenOverlay || inModal) return false;
  return true;
}

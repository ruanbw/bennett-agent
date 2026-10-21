/**
 * 登出确认框的说明文案。
 *
 * 这段话是数据丢失前的最后一道提醒，不能按「先渲染默认文案、异步算完再换」处理：
 * 用户可能在数量回来之前就点了确认，而默认文案根本没提「有 N 条未同步」。所以调用方
 * 在数量未知时应当禁用确认，文案也要明确说「正在检查」。
 */

export function logoutWarning(unsynced: number | null, countFailed: boolean): string {
  if (countFailed) {
    return '无法读取本机待同步数量。登出仍会清空本机数据，请确认重要内容已经同步。';
  }
  if (unsynced === null) {
    return '正在检查本机是否有内容尚未同步到服务器…';
  }
  if (unsynced > 0) {
    return `有 ${unsynced} 条内容尚未同步到服务器，登出会清空本机数据，这些内容将无法恢复。`;
  }
  return '登出会清空本机保存的会话记录，之后需要重新输入邮箱和密码才能继续对话。';
}

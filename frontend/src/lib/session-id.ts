/**
 * 会话标识的线上形状。
 *
 * 三个请求都收 `session_id`（`/api/chat`、沙箱心跳、沙箱状态），它们的校验必须与后端
 * `SESSION_ID_PATTERN` 完全一致。**只在这里定义一份**：两份正则迟早会分叉，而分叉的
 * 表现是「同步接口接受、聊天接口 422」——用户整个会话发不出消息，是最贵的失败方式。
 *
 * 字符集允许 `_`：旧版本生成过 `sess_<时间戳>_<随机>`，这些 id 还留在用户的本地库里，
 * 并且一直能通过同步接口（`app/schemas/sync.py:ID_PATTERN` 同样允许 `_`）。
 */
export const SESSION_ID_PATTERN = /^sess_[A-Za-z0-9_-]{1,59}$/;

/**
 * 这个 id 能不能作为 `session_id` 发出去。
 *
 * 不匹配时调用方**不带**这个字段，而不是发出去赌服务端放行：服务端的校验失败是请求级
 * 的 422，整轮对话都会失败；不带字段则只是沙箱工具回「当前会话缺少会话标识」，其余
 * 能力照常。所以这里守的是「别把聊天打挂」，不是「保证沙箱可用」。
 */
export function isSessionIdSendable(sessionId: string): boolean {
  return SESSION_ID_PATTERN.test(sessionId);
}

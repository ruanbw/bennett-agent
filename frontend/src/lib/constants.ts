/**
 * 全应用共享的常量与 id 生成。
 *
 * 收敛 R-4/O-4：此前这些裸值散落在各文件（`slice(-30)`、`800`、
 * `Date.now()+Math.random()` 的两套 id 方案），改一处要翻多个文件，
 * 且「历史保留多少条」这类知识在前端单方面决定、后端无从知晓。
 */

import { PREFERRED_LEVEL } from './reasoning-effort'

// ---------------------------------------------------------------------
// 后端地址
// ---------------------------------------------------------------------
/**
 * 后端 API 基址，来自 `frontend/.env` 的 `VITE_API_BASE_URL`。
 *
 * 前后端分离后前端不再由后端托管，接口地址与页面来源无关，必须显式配置。
 * 去掉结尾的 `/`，避免拼出 `//api/chat`——FastAPI 会把它当成另一个路径返回 404。
 */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

// ---------------------------------------------------------------------
// 密码策略
// ---------------------------------------------------------------------
/**
 * 密码最小长度，与后端 `app/services/auth.py:MIN_PASSWORD_LENGTH` 保持一致。
 *
 * 放在 constants 而不是 auth.ts：`auth.ts` 依赖 db/api，而登录页、改密校验都要这个
 * 数字；让纯校验模块去 import auth 会把一整条网络/存储链路拖进单元测试。
 * 前端校验只是**即时反馈**，不是安全边界——真正的校验在服务端。
 */
export const MIN_PASSWORD_LENGTH = 8

// ---------------------------------------------------------------------
// 对话上下文
// ---------------------------------------------------------------------

/**
 * 送往模型的历史消息条数上限。
 *
 * 与后端 `app/schemas/chat.py:MAX_HISTORY_MESSAGES`（100）是两回事：后端那是个
 * 安全上限，这里是个成本/质量取舍（更长的历史更贵且更容易跑偏）。
 */
export const HISTORY_LIMIT = 30

/**
 * 系统提示块（失败 / 断流 / 跨标签冲突）在正文里的起始标记。
 *
 * 提示是拼在正文尾部的（`chat-store` 的 `appendNotice` 与几处事件折叠），而正文有三个
 * 出口都会把它当成回答的一部分带出去：回放给模型（`toReplayable`）、复制回答、导出
 * Markdown。**写入端与剥离端必须共用这一个常量**：两边各写一遍字面量时，改格式只改
 * 一处不会有任何报错，只会静默漏剥——模型于是看到上一轮「自己」用人类口吻写的告警。
 *
 * 剥离实现见 `message-copy.ts:stripNotices`。
 */
export const NOTICE_PREFIX = '> ⚠️ **'

// ---------------------------------------------------------------------
// 流式落库
// ---------------------------------------------------------------------
/** 流式过程中落库的节流间隔（ms）：刷新或关页面时不至于丢掉整段回复。 */
export const PARTIAL_FLUSH_INTERVAL_MS = 800

/**
 * 渲染通知的合并窗口（ms）。
 *
 * store 在每个增量事件后都要通知订阅者，但同一帧内渲染多次毫无意义。
 * 0 表示「下一个宏任务」——既不会拖慢首字显示，又能把一次网络分片里的多个
 * 增量合并成一次渲染。
 */
export const NOTIFY_COALESCE_MS = 0

// ---------------------------------------------------------------------
// 会话列表
// ---------------------------------------------------------------------
export const DEFAULT_SESSION_TITLE = '新对话'

/**
 * 新会话的默认思考等级。
 *
 * 与 `reasoning-effort.ts` 的 `PREFERRED_LEVEL` 是同一个值，且只能有一个定义：
 * 两处各写一遍 'medium' 时，改了其中一处就会变成「新会话默认等级」与「切换模型后
 * 的回退等级」不一致，而不会有人报错。
 */
export const DEFAULT_REASONING_EFFORT = PREFERRED_LEVEL

/** 生成标题失败时用首条消息内容兜底截断的长度。 */
export const FALLBACK_TITLE_CHARS = 20

// ---------------------------------------------------------------------
// id 生成
// ---------------------------------------------------------------------
/**
 * 生成带前缀的唯一 id。
 *
 * 此前是两套手写方案（会话用 `sess_${Date.now()}_${随机}`，消息用
 * `msg_${Date.now()}_${随机}` 加 `a` 后缀区分用户/助手），既重复又依赖
 * 「同一毫秒内 Math.random 不碰撞」这个未言明的假设。
 *
 * `crypto.randomUUID` 在非安全上下文（http 且非 localhost）不可用，所以保留
 * 随机兜底——本应用常见于「局域网 http 访问」，那个场景下它不是边缘情况。
 */
export function newId(prefix: string): string {
  const uuid =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID().replaceAll('-', '')
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
  return `${prefix}_${uuid}`
}

export const newSessionId = (): string => newId('sess')
export const newMessageId = (): string => newId('msg')

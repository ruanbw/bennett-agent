import Dexie, { type Table } from 'dexie';

import type { MessageRecord, SessionRecord } from '../types';

/**
 * 本地会话与消息存储。
 *
 * messages 记录字段：
 *   id / sessionId / role / createdAt  —— 索引字段；content / thinking / thinkingMs /
 *   toolCalls / stopped / incomplete / clarification —— 非索引字段，用于 UI 展示
 *
 * ## 关于版本链
 *
 * Dexie 的版本声明是**追加式历史**：已发布的版本声明不能回改，否则已安装旧版本的
 * 客户端会走错升级路径。因此下面保留了若干「当时的改动看不太出来」的版本：
 *
 *   v1  初版
 *   v2  为老记录回填 thinkingMs / stopped 默认值
 *   v3  会话加 webSearch 索引
 *   v4  无 schema 变化（当时只是想给消息加 toolCalls 字段，而新增非索引字段
 *       不需要新版本——Dexie 的 stores() 只管索引）
 *   v5  无 schema 变化，占位保持链连续
 *   v6  会话加 reasoningEffort / isPinned
 *   v7  摘掉已废弃的 webSearch 索引
 *
 * v4/v5 是无操作版本，但删掉它们会改变已安装客户端的升级路径，所以留着并在此说明。
 * 教训是：**只有索引变化才需要新版本**，新增普通字段不需要。
 */
export class ChatDatabase extends Dexie {
  // 显式声明表类型：否则 `db.messages.update(id, changes)` 的 changes 是 any，
  // 写错字段名不会被发现。
  sessions!: Table<SessionRecord, string>;
  messages!: Table<MessageRecord, string>;

  constructor() {
    super('BennettAgentDB');

    this.version(1).stores({
      sessions: 'id, title, model, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    this.version(2)
      .stores({
        sessions: 'id, title, model, createdAt, updatedAt',
        messages: 'id, sessionId, role, createdAt',
      })
      .upgrade((tx) =>
        tx
          .table('messages')
          .toCollection()
          .modify((message: MessageRecord) => {
            if (typeof message.stopped !== 'boolean') message.stopped = false;
            if (typeof message.thinkingMs !== 'number') message.thinkingMs = 0;
          })
      );

    this.version(3).stores({
      sessions: 'id, title, model, webSearch, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    this.version(4).stores({
      sessions: 'id, title, model, webSearch, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    this.version(5).stores({
      sessions: 'id, title, model, webSearch, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    this.version(6)
      .stores({
        sessions: 'id, title, model, reasoningEffort, webSearch, isPinned, createdAt, updatedAt',
        messages: 'id, sessionId, role, createdAt',
      })
      .upgrade((tx) =>
        tx
          .table('sessions')
          .toCollection()
          .modify((session: SessionRecord) => {
            if (typeof session.isPinned !== 'number') {
              session.isPinned = session.isPinned ? 1 : 0;
            }
          })
      );

    // v7：摘掉 webSearch 索引。
    // 该索引来自「显式联网开关」——那个功能已被移除（工具可用性改由后端配置决定），
    // 字段既没人读也没人写，索引却一直留在声明里。
    this.version(7).stores({
      sessions: 'id, title, model, reasoningEffort, isPinned, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    // 这里**刻意不**自己监听 versionchange。
    //
    // Dexie 构造时会注册内建处理器，用 `close({ disableAutoOpen: false })` 关连接：
    // 下次读写会自动按新版本重开。此前这里额外挂了一个 `this.close()`，而 close 默认
    // `disableAutoOpen: true`——它在内建处理器之后执行，等于把自动重开永久关死。于是
    // 其他标签页升级 schema 后，本标签页的读写全部 reject（会话列表显示为空）。
    // 需要提示用户时用 `blocked` 事件即可，不要碰 versionchange 的默认语义。

    this.on('blocked', () => {
      console.warn('数据库升级被阻挡，请关闭其他标签页');
    });
  }
}

export const db = new ChatDatabase();

/**
 * 清空全部本地会话与消息（登出、换账号时用）。
 *
 * 必须放在一个事务里：分开 clear 的话，中途失败会留下「会话没了消息还在」的
 * 半清空状态，下一次加载要么显示孤立的空会话，要么读到不属于当前账号的残留。
 */
export async function clearAllLocalData(): Promise<void> {
  await db.transaction('rw', db.sessions, db.messages, async () => {
    await db.messages.clear();
    await db.sessions.clear();
  });
}

/**
 * 按 id 批量删除消息（发送失败时回滚刚写入的用户/助手占位消息）。
 *
 * 用 bulkDelete 一次成批删除：逐条 delete 会在失败中留下只回滚了一半的消息。
 */
export async function deleteMessages(ids: string[]): Promise<void> {
  if (ids.length === 0) return;
  await db.messages.bulkDelete(ids);
}

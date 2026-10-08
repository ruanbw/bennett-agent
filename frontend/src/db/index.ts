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

    this.on('versionchange', () => {
      this.close();
    });

    this.on('blocked', () => {
      console.warn('数据库升级被阻挡，请关闭其他标签页');
    });
  }
}

export const db = new ChatDatabase();

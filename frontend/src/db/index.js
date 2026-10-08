import Dexie from 'dexie';

// messages 记录字段：
//   id / sessionId / role / content / thinking / createdAt  —— 索引字段
//   thinkingMs 思考耗时(ms)、stopped 是否被中止 —— 非索引字段，用于 UI 展示
export class ChatDatabase extends Dexie {
  constructor() {
    super('BennettAgentDB');

    this.version(1).stores({
      sessions: 'id, title, model, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    // v2：显式版本化消息表，并为老记录回填 thinkingMs / stopped 默认值
    this.version(2)
      .stores({
        sessions: 'id, title, model, createdAt, updatedAt',
        messages: 'id, sessionId, role, createdAt',
      })
      .upgrade((tx) =>
        tx
          .table('messages')
          .toCollection()
          .modify((message) => {
            if (typeof message.stopped !== 'boolean') message.stopped = false;
            if (typeof message.thinkingMs !== 'number') message.thinkingMs = 0;
          })
      );

    // v3：会话支持记录 webSearch 联网状态与消息记录 webSearch 元数据
    this.version(3).stores({
      sessions: 'id, title, model, webSearch, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    // v4：消息支持保存 toolCalls 结构
    this.version(4).stores({
      sessions: 'id, title, model, webSearch, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    // v5：占位版本，保持 Dexie 历史升级链连续性（避免从 v4 升级的用户抛错）
    this.version(5).stores({
      sessions: 'id, title, model, webSearch, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    });

    // v6：会话支持置顶 isPinned (0 或 1，方便按 isPinned, updatedAt 排序)
    this.version(6).stores({
      sessions: 'id, title, model, reasoningEffort, webSearch, isPinned, createdAt, updatedAt',
      messages: 'id, sessionId, role, createdAt',
    }).upgrade((tx) =>
      tx
        .table('sessions')
        .toCollection()
        .modify((session) => {
          if (typeof session.isPinned !== 'number') {
            session.isPinned = session.isPinned ? 1 : 0;
          }
        })
    );

    this.on('versionchange', () => {
      this.close();
    });

    this.on('blocked', () => {
      console.warn('数据库升级被阻挡，请关闭其他标签页');
    });
  }
}

export const db = new ChatDatabase();

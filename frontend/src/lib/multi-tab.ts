/**
 * 多标签页协调（C9）。
 *
 * 同一会话在两个标签页里各有一份内存 run，各自以「整段覆写」的方式写同一条助手消息，
 * 库里的内容会在两种回答之间来回翻转；一侧删除会话，另一侧也毫不知情。这里只提供两个
 * 最小原语，且不引入任何跨页状态——真相仍然只有 IndexedDB 一份，通知只是「去重读」：
 *
 *   - `acquireRunLock`：生成互斥。用 Web Locks 而不是自建心跳/时间戳租约——锁随标签页
 *     被强杀、刷新自动释放，不需要超时兜底，也就不会出现「租约没续上，别人以为你在跑」。
 *   - `notifySessionsChanged` / `subscribeSessionsChanged`：失效通知。只广播「哪个会话
 *     被改过」，不带内容：收到通知的一侧自己去库里重读，两套内存态因此不会互相覆盖。
 *
 * 这里用到的浏览器 API 都可能缺席（老浏览器、node 测试）或抛错（隐私模式、策略禁用），
 * 因此全部分支都吞掉异常：跨标签同步是尽力而为的增强，任何一步失败都不该让本页的生成、
 * 落库、会话列表跟着挂掉。
 */

import { newId } from './constants';

const SESSIONS_CHANNEL = 'bennett:sessions';
const RUN_LOCK_PREFIX = 'bennett:chat-run:';

/**
 * 本页标识。
 *
 * 真实 BroadcastChannel 不会把消息回传给发送方，但「自己发的通知必须忽略」是订阅端的
 * 契约（测试里的假实现就按「谁都能收到」来验它），所以消息里必须带来源。
 */
export const TAB_ID = newId('tab');

/** 一条失效通知：只带 id，不带内容。 */
export interface SessionsChangedMessage {
  sessionId: string;
  sourceId: string;
}

/** 一次生成结束可能连着发多条通知（标题占位、标题定稿、终态落库），合并成一个窗口。 */
export const SESSIONS_CHANGED_DEBOUNCE_MS = 300;

// ---------------------------------------------------------------------
// 失效通知
// ---------------------------------------------------------------------
type BroadcastChannelCtor = new (name: string) => BroadcastChannel;

/**
 * 取全局 BroadcastChannel 构造函数。
 *
 * 走 `globalThis` 而不是裸标识符：node（vitest）与老浏览器里它可能不存在，
 * 直接引用会 ReferenceError。测试也靠替换这个全局来注入假实现。
 */
function channelCtor(): BroadcastChannelCtor | null {
  const ctor: unknown = (globalThis as { BroadcastChannel?: unknown }).BroadcastChannel;
  return typeof ctor === 'function' ? (ctor as BroadcastChannelCtor) : null;
}

/**
 * Node 的 BroadcastChannel 会一直占用事件循环（vitest 里表现为进程不退出），能 unref
 * 就 unref：浏览器里没有这个方法，可选调用对两边都安全。
 */
function unrefChannel(channel: BroadcastChannel): void {
  (channel as { unref?: () => void }).unref?.();
}

/**
 * 发送用的通道，按构造函数缓存。
 *
 * 不「发一条建一个再关掉」：关闭与投递完成的先后不在我们的控制范围内。缓存以构造函数
 * 为键是为了测试——把全局换成假实现后必须用新实例，不能被上一个用例的通道串味。
 */
let postChannel: { ctor: BroadcastChannelCtor; channel: BroadcastChannel } | null = null;

function postToOtherTabs(message: SessionsChangedMessage): void {
  const ctor = channelCtor();
  if (!ctor) return;

  try {
    if (!postChannel || postChannel.ctor !== ctor) {
      const channel = new ctor(SESSIONS_CHANNEL);
      unrefChannel(channel);
      postChannel = { ctor, channel };
    }
    postChannel.channel.postMessage(message);
  } catch (err) {
    console.warn('跨标签会话通知发送失败:', err);
  }
}

/** 广播「这个会话变了」。没有 BroadcastChannel 时是空操作。 */
export function notifySessionsChanged(sessionId: string): void {
  if (!sessionId) return;
  postToOtherTabs({ sessionId, sourceId: TAB_ID });
}

export type SessionsChangedListener = (message: SessionsChangedMessage) => void;

/**
 * 订阅其他标签页的失效通知，返回解绑函数。
 *
 * 自己发的消息直接丢弃：写入方已经知道发生了什么，再重读一次只是白扫一遍库。
 * 没有 BroadcastChannel 时返回空解绑函数，调用方不需要分支判断。
 */
export function subscribeSessionsChanged(listener: SessionsChangedListener): () => void {
  const ctor = channelCtor();
  if (!ctor) return () => {};

  let channel: BroadcastChannel;
  try {
    channel = new ctor(SESSIONS_CHANNEL);
    unrefChannel(channel);
    channel.onmessage = (event: MessageEvent) => {
      const message = parseSessionsChanged(event.data);
      if (!message || message.sourceId === TAB_ID) return;
      try {
        listener(message);
      } catch (err) {
        // 订阅方的异常不能反过来打断通道
        console.error('处理跨标签会话变更失败:', err);
      }
    };
  } catch (err) {
    console.warn('跨标签会话通知订阅失败:', err);
    return () => {};
  }

  return () => {
    try {
      channel.close();
    } catch (err) {
      console.warn('关闭跨标签会话通道失败:', err);
    }
  };
}

/**
 * 校验外来消息的形状。
 *
 * 同一频道名可能被别的版本/别的页面的代码占用，不能假设 `event.data` 一定是我们发的
 * 那个对象；形状不对就当没收到。
 */
function parseSessionsChanged(data: unknown): SessionsChangedMessage | null {
  if (typeof data !== 'object' || data === null) return null;
  const { sessionId, sourceId } = data as Partial<SessionsChangedMessage>;
  if (typeof sessionId !== 'string' || !sessionId) return null;
  if (typeof sourceId !== 'string' || !sourceId) return null;
  return { sessionId, sourceId };
}

// ---------------------------------------------------------------------
// 生成互斥
// ---------------------------------------------------------------------
/** 只声明用到的那部分 Web Locks 形状：node 与 DOM 两套类型都能套上。 */
export interface LockManagerLike {
  request(
    name: string,
    options: { ifAvailable: boolean },
    callback: (lock: unknown) => unknown,
  ): Promise<unknown>;
}

export type RunLock =
  /** 拿到了锁：`release` 必须在该 run 结束时调用（executeRun 的 finally）。 */
  | { kind: 'held'; release: () => void }
  /** 另一个标签页正在同一会话里生成：本次 run 应立即以终端错误收尾。 */
  | { kind: 'busy' }
  /** 没有可用的 Web Locks：退回改动前的「无互斥」行为。 */
  | { kind: 'unsupported' };

function lockManager(): LockManagerLike | null {
  const locks: unknown = (globalThis as { navigator?: { locks?: unknown } }).navigator?.locks;
  return typeof (locks as LockManagerLike | undefined)?.request === 'function'
    ? (locks as LockManagerLike)
    : null;
}

/**
 * 尝试占用「某会话正在生成」这把锁。
 *
 * `ifAvailable: true`：拿不到就立刻返回，绝不排队等待——排队会让第二个标签页的 run 在
 * 第一个结束之后才开始生成，用户看到的是「点了没反应，过一会儿凭空冒出一条回复」。
 * 拿不到锁时浏览器仍会调用回调（参数为 null），据此判定。
 *
 * 返回值是判定结果而不是锁本身：调用方（executeRun）只需要「拿不到 → 取消」与
 * 「拿到了 → 结束时释放」两件事。
 */
export async function acquireRunLock(sessionId: string): Promise<RunLock> {
  const locks = lockManager();
  if (!locks) return { kind: 'unsupported' };

  let settle: ((result: RunLock) => void) | null = null;
  const decided = new Promise<RunLock>((resolve) => {
    settle = resolve;
  });

  try {
    // 有意不 await：request 的 promise 要等锁释放才结算，await 它等于把生成挂住
    void locks
      .request(`${RUN_LOCK_PREFIX}${sessionId}`, { ifAvailable: true }, (lock) => {
        if (!lock) {
          settle?.({ kind: 'busy' });
          return undefined;
        }
        // 占着不放：直到 release 被调用，request 的 promise 才结算，锁也随之交还
        return new Promise<void>((releaseLock) => {
          settle?.({ kind: 'held', release: releaseLock });
        });
      })
      .catch((err: unknown) => {
        // 锁管理器异常（策略禁用等）不该堵死生成：按「没有互斥能力」处理
        console.warn('跨标签生成锁请求失败:', err);
        settle?.({ kind: 'unsupported' });
      });
  } catch (err) {
    console.warn('跨标签生成锁请求失败:', err);
    return { kind: 'unsupported' };
  }

  return decided;
}

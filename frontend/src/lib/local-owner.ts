/**
 * 本地缓存归属：这台浏览器里的会话/消息属于哪个账号。
 *
 * ## 为什么需要它
 *
 * 会话存在 IndexedDB 里、按 origin 隔离，**不跟账号走**。原来的实现只在「用户点登出」
 * 时清库；会话**过期**（任何请求 401）只是把 React 状态切回未登录，库里那一家人的
 * 对话（含待推送的行）原封不动留着。下一个在这台机器上登录的人——哪怕是另一个
 * 账号——会先看到前一个人的会话列表，同步引擎还会把前一个人没推上去的行**推进新账号**。
 * 这是跨账号数据泄漏，不是显示问题。
 *
 * ## 规则
 *
 * - 登录/注册/启动探测确认身份后，调用 `claimLocalData(userId)`：
 *   归属一致 → 什么都不做（刷新页面、重新登录同一账号都保留历史）；
 *   归属不一致 → 先清空本地库，再写入新归属（`cleared: true`）。
 * - **会话过期不清库**。401 不等于用户要换账号，盲目清库会把还没同步的内容直接删掉；
 *   保留下来是安全的，因为下一次登录会先核对归属。
 * - 没有归属记录的老库（本模块上线前建的）按同步元数据里的 `userId` 推断：游标和回填
 *   标记都是同步引擎按账号写入的。两者都没有时才「认领」现有数据——这是唯一能保住
 *   用户历史的选择。
 *
 * 认领必须**串行**：登录流程和启动探测可能同时到达（StrictMode 双执行、跨标签页换号），
 * 并发执行会让两边都读到「无归属」然后各自写一遍，甚至一边清库一边读。
 */

import { clearAllLocalData, db } from '../db';
import { clearComposerDrafts } from './composer-draft';
import { notifyAccountSwitched } from './multi-tab';

/** syncMeta 里的归属键。与同步引擎的 `cursor` / `backfill` 同表、不冲突。 */
export const LOCAL_OWNER_KEY = 'owner';

/** 归属确认失败时的提示：失败要挡住登录，否则可能在别人的缓存上继续写。 */
export const LOCAL_CACHE_UNAVAILABLE_MESSAGE =
  '无法确认本机缓存归属（可能被其他标签页占用），请关闭其他标签页后重试';

export interface ClaimResult {
  /** 是否因为换了账号而清空了本地缓存。 */
  cleared: boolean;
  /** 认领前的归属；`null` = 之前没有归属记录（老库或全新安装）。 */
  previousOwner: string | null;
}

/**
 * 读取本地缓存的归属。
 *
 * 显式记录优先；没有时退回同步元数据里的 `userId`——同步游标只有真正同步过的账号才会
 * 写，它比「无主」更能说明这份数据是谁的。
 */
export async function readLocalOwner(): Promise<string | null> {
  const explicit = await db.syncMeta.get(LOCAL_OWNER_KEY);
  if (explicit?.userId) return explicit.userId;

  for (const key of ['cursor', 'backfill']) {
    const record = await db.syncMeta.get(key);
    if (record?.userId) return record.userId;
  }
  return null;
}

async function writeOwner(userId: string): Promise<void> {
  await db.syncMeta.put({ key: LOCAL_OWNER_KEY, userId, value: userId, updatedAt: Date.now() });
}

async function claimOnce(userId: string): Promise<ClaimResult> {
  const previousOwner = await readLocalOwner();
  if (previousOwner === userId) return { cleared: false, previousOwner };

  // 没有归属记录时不删：这份数据可能就是当前用户自己的（本模块上线前留下的）。
  const cleared = previousOwner !== null;
  if (cleared) {
    console.warn(`本地缓存属于 ${previousOwner ?? '未知账号'}，切换为 ${userId} 前已清空`);
    await clearAllLocalData();
    // 草稿在 localStorage，不在 IndexedDB；不删会在新账号的输入框里露出上一个人的文本。
    clearComposerDrafts();
  }
  await writeOwner(userId);
  // 写完全新归属再通知其他标签页：它们收到通知会立刻重新探测并认领，必须能读到新归属。
  if (cleared) notifyAccountSwitched();
  return { cleared, previousOwner };
}

/** 串行队列的尾巴：认领按到达顺序逐个执行。 */
let claimTail: Promise<void> = Promise.resolve();

/**
 * 确认本地缓存归 `userId` 所有，必要时先清空。
 *
 * 调用点必须在「把状态切成已登录 / 启动同步引擎」**之前** await 它：反过来的话，
 * 新账号的同步会先跑起来，把旧账号的待推送行当成自己的推上去。
 *
 * 抛错表示无法确认归属（IndexedDB 被其他标签页的升级阻塞、配额耗尽等）。调用方应当
 * **拒绝进入已登录状态**：宁可用户重试，也不要在别人的缓存上继续读写。
 */
export function claimLocalData(userId: string): Promise<ClaimResult> {
  const result = claimTail.then(() => claimOnce(userId));
  // 失败不阻塞后续认领：一次配额错误不该让之后所有登录都卡在同一个 rejected promise 上。
  claimTail = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

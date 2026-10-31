import { describe, expect, it, vi } from 'vitest';
import { createMessageQueue, type MessageQueueController } from '@assistant-ui/react';

/**
 * ChatProvider 队列装配所依赖的 `createMessageQueue` 行为契约。
 *
 * ChatProvider 的队列逻辑本身是 React 装配层（busy/idle 边沿接在 run 生死上），
 * 抽不出纯函数；这里直接对第三方队列实现立契约，把「我们依赖它做什么」写成
 * 可执行的规格——库升级破坏任何一条时，这里的失败会先于用户看到。
 *
 * 契约与 ChatProvider 的用法一一对应：
 *   - enqueue：空闲（没有 run 在占位）→ 立即派发并占住 running 位；busy 边沿后 → 入队等待；
 *   - notifyIdle：本轮结束放行**一条**（多余调用不跳条）；
 *   - remove：等待中的消息可以删；已派发的是 no-op；
 *   - move(lane:'steer')：「立即发送」——不打断本轮（无 cancel），但排在下一轮最先；
 *   - edit：等待中的消息可以改文字；
 *   - notifyCancelled：暂停推进，下一条 enqueue 解除。
 */

interface QueueFixture {
  controller: MessageQueueController;
  dispatched: string[];
}

function makeQueue(onRun?: (controller: MessageQueueController) => void): QueueFixture {
  const dispatched: string[] = [];
  let controller: MessageQueueController;
  controller = createMessageQueue({
    run: (message) => {
      dispatched.push((message.content[0] as { text?: string }).text ?? '');
      onRun?.(controller);
    },
  });
  return { controller, dispatched };
}

const append = (text: string) =>
  ({
    parentId: null,
    sourceId: null,
    role: 'user',
    content: [{ type: 'text', text }],
    attachments: [],
    createdAt: new Date(),
    metadata: { custom: {} },
    runConfig: {},
  }) as Parameters<MessageQueueController['adapter']['enqueue']>[0];

describe('createMessageQueue 契约（ChatProvider 插入排队的依赖面）', () => {
  it('空闲时 enqueue 立即派发第一条，连发第二条要等 notifyIdle', () => {
    const { controller, dispatched } = makeQueue();
    controller.adapter.enqueue(append('第一条'));
    expect(dispatched).toEqual(['第一条']);

    controller.adapter.enqueue(append('第二条'));
    expect(dispatched).toEqual(['第一条']);

    controller.notifyIdle();
    expect(dispatched).toEqual(['第一条', '第二条']);
    expect(controller.adapter.items).toHaveLength(0);
  });

  it('busy 边沿后的 enqueue 只入队不派发；notifyIdle 每次只放行一条', () => {
    const { controller, dispatched } = makeQueue();
    controller.notifyBusy();
    controller.adapter.enqueue(append('甲'));
    controller.adapter.enqueue(append('乙'));
    expect(dispatched).toEqual([]);
    expect(controller.adapter.items.map((i) => i.prompt)).toEqual(['甲', '乙']);

    controller.notifyIdle();
    expect(dispatched).toEqual(['甲']);

    // 多余的 idle（订阅通知、其他收尾路径）不能跳条
    controller.notifyIdle();
    controller.notifyIdle();
    expect(dispatched).toEqual(['甲', '乙']);
  });

  it('等待中的消息可以删除；派发后的同 id 删除是 no-op', () => {
    const { controller, dispatched } = makeQueue();
    controller.notifyBusy();
    controller.adapter.enqueue(append('留着'));
    const keptId = controller.adapter.items[0]!.id;
    controller.adapter.enqueue(append('删掉'));
    const removedId = controller.adapter.items[1]!.id;

    controller.adapter.remove(removedId);
    expect(controller.adapter.items.map((i) => i.prompt)).toEqual(['留着']);

    controller.notifyIdle();
    expect(dispatched).toEqual(['留着']);
    // 队列里已无此 id：再删是 no-op，不抛错
    expect(() => controller.adapter.remove(removedId)).not.toThrow();
    expect(() => controller.adapter.remove(keptId)).not.toThrow();
  });

  it('「立即发送」= move 到 steer 位：不打断本轮，但下一轮最先发', () => {
    const { controller, dispatched } = makeQueue();
    // 「先问的」已经在生成（busy 边沿后它从队头派发、正在跑），「插队的」在队列里等
    controller.notifyBusy();
    controller.adapter.enqueue(append('先问的'));
    controller.adapter.enqueue(append('插队的'));
    controller.notifyIdle(); // 「先问的」被派发（running 位被占住，队列只剩「插队的」）
    expect(dispatched).toEqual(['先问的']);
    expect(controller.adapter.items.map((i) => i.prompt)).toEqual(['插队的']);

    // 用户点「立即发送」：移到 steer 位
    const jumpId = controller.adapter.steerItems.length
      ? controller.adapter.steerItems[0]!.id
      : controller.adapter.items[0]!.id;
    controller.adapter.move(jumpId, { lane: 'steer' });
    expect(controller.adapter.items.map((i) => i.prompt)).toEqual([]);
    expect(controller.adapter.steerItems.map((i) => i.prompt)).toEqual(['插队的']);

    // 本轮结束：steer 位优先派发，不打断已产出的回答
    controller.notifyBusy();
    controller.notifyIdle();
    expect(dispatched).toEqual(['先问的', '插队的']);
  });

  it('等待中的消息可以编辑文字，派发时用新内容', () => {
    const { controller, dispatched } = makeQueue();
    controller.notifyBusy();
    controller.adapter.enqueue(append('旧问题'));
    const id = controller.adapter.items[0]!.id;
    controller.adapter.edit(id, append('新问题'));

    controller.notifyIdle();
    expect(dispatched).toEqual(['新问题']);
  });

  it('notifyCancelled 暂停推进：取消一轮后队列原地保留，直到下次 enqueue', () => {
    const { controller, dispatched } = makeQueue();
    // 第一轮正常跑：busy 边沿入队、idle 边沿派发
    controller.notifyBusy();
    controller.adapter.enqueue(append('这轮在跑'));
    controller.notifyIdle();
    expect(dispatched).toEqual(['这轮在跑']);

    // 第二轮开始（busy 边沿），用户排队了一条后点了停止
    controller.notifyBusy();
    controller.adapter.enqueue(append('排队的下一条'));
    controller.notifyCancelled();
    controller.notifyIdle(); // 本轮收尾：暂停推进，队头不放行
    expect(dispatched).toEqual(['这轮在跑']);
    expect(controller.adapter.items.map((i) => i.prompt)).toEqual(['排队的下一条']);

    // 用户再次发送：解除暂停，先放行老排队项
    controller.adapter.enqueue(append('停完之后发的新消息'));
    expect(dispatched).toEqual(['这轮在跑', '排队的下一条']);
    controller.notifyIdle();
    expect(dispatched).toEqual(['这轮在跑', '排队的下一条', '停完之后发的新消息']);
  });

  it('driver.run 同步抛错（未起跑）时消息还原回队列原位置', () => {
    const controller = createMessageQueue({
      run: () => {
        throw new Error('槽位被抢');
      },
    });
    controller.hold(); // 先 hold：enqueue 只入队不派发
    controller.adapter.enqueue(append('会被还原的'));
    // release 触发 advance → driver.run 同步抛错：异常沿 release 抛给调用方
    expect(() => controller.release()).toThrow('槽位被抢');
    // 但消息已还原回队列原位置：等下一次推进（比如新一轮收尾的 notifyIdle）会再试
    expect(controller.adapter.items.map((i) => i.prompt)).toEqual(['会被还原的']);
  });

  it('hold/release：装配期间入队的消息在 release 前不派发', () => {
    const { controller, dispatched } = makeQueue();
    controller.hold();
    controller.adapter.enqueue(append('憋着'));
    expect(dispatched).toEqual([]);

    controller.release();
    expect(dispatched).toEqual(['憋着']);
  });

  it('运行中的队列订阅在每次 items 变化时被通知（composer.queue 的数据源）', () => {
    const { controller } = makeQueue();
    const listener = vi.fn();
    const unsubscribe = controller.subscribe(listener);

    controller.notifyBusy();
    controller.adapter.enqueue(append('通知我'));
    expect(listener).toHaveBeenCalled();

    listener.mockClear();
    unsubscribe();
    controller.adapter.enqueue(append('不再通知'));
    expect(listener).not.toHaveBeenCalled();
  });
});

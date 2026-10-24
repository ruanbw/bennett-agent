import { afterEach, describe, expect, it, vi } from 'vitest';

import { pendingApproval } from './chat-message';
import type { RunState } from './chat-store';
import type { ApprovalState, RuntimeMessage } from '../types';

/**
 * 审批相关的前端契约：
 *
 * 1. `pendingApproval` 只从消息列表推导（不存 `handled` 标记），否则刷新后
 *    审批卡片会消失，那一轮被拦下的调用永久卡死；
 * 2. 决定必须能回到请求体里，且**缺失的决定不会被当成批准**；
 * 3. 续跑历史以「带工具调用的助手消息」结尾，且那条消息由快照构造（不读库，
 *    避免与「清掉 approval」形成读写竞态）。
 */

/** 会话标识：形状要与服务端的 `^sess_[A-Za-z0-9_-]{1,59}$` 一致（见 `session-id.ts`）。 */
const SESSION_ID = 'sess_frontendtest0001';

const approval: ApprovalState = {
  toolCalls: [
    {
      toolCallId: 'c1',
      toolName: 'delete_document',
      argsText: '{"id": 42}',
      description: '删除一篇知识库文档（不可撤销）',
      riskLevel: 'dangerous',
    },
  ],
};

function assistantWith(approvalState: ApprovalState | null): RuntimeMessage {
  return {
    id: 'a1',
    role: 'assistant',
    content: [],
    createdAt: new Date(0),
    metadata: { custom: { stopped: false, thinkingMs: 0, clarification: null, approval: approvalState } },
  };
}

const userMessage: RuntimeMessage = {
  id: 'u1',
  role: 'user',
  content: [{ type: 'text', text: '删掉它' }],
  createdAt: new Date(0),
};

describe('pendingApproval', () => {
  it('从最后一条 assistant 消息推导待审批调用', () => {
    const pending = pendingApproval([userMessage, assistantWith(approval)], false);

    expect(pending?.messageId).toBe('a1');
    expect(pending?.toolCalls).toHaveLength(1);
    expect(pending?.toolCalls[0].toolName).toBe('delete_document');
  });

  it('生成中不推导：审批请求到达时本轮已结束，但抢先弹卡片会让用户点不动', () => {
    expect(pendingApproval([userMessage, assistantWith(approval)], true)).toBeNull();
  });

  it('最后一条不是 assistant 时不推导（用户已经发了新消息 → 自然消解）', () => {
    expect(pendingApproval([assistantWith(approval), userMessage], false)).toBeNull();
  });

  it('没有 approval 字段时不推导', () => {
    expect(pendingApproval([userMessage, assistantWith(null)], false)).toBeNull();
  });

  it('空 toolCalls 不推导（避免渲染一张没有任何内容的卡片）', () => {
    expect(pendingApproval([userMessage, assistantWith({ toolCalls: [] })], false)).toBeNull();
  });

  it('消息列表为空时不炸', () => {
    expect(pendingApproval(undefined, false)).toBeNull();
    expect(pendingApproval([], false)).toBeNull();
  });
});

describe('请求体里的审批决定', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('把决定按 tool_call_id 发出去', async () => {
    const { streamChat } = await import('./chat-transport');
    const bodies: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        bodies.push(String(init.body));
        return new Response('', { status: 200, headers: { 'content-type': 'text/event-stream' } });
      }),
    );

    const controller = new AbortController();
    // 消费流：这里只关心请求体，事件内容无所谓
    for await (const _ of streamChat({
      sessionId: SESSION_ID,
      model: 'm',
      reasoningEffort: null,
      messages: [{ role: 'user', content: 'hi' }],
      approvals: { c1: true, c2: false },
      signal: controller.signal,
    })) {
      void _;
    }

    expect(JSON.parse(bodies[0]).approvals).toEqual({ c1: true, c2: false });
  });

  it('没有决定时不带 approvals 字段', async () => {
    const { streamChat } = await import('./chat-transport');
    const bodies: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        bodies.push(String(init.body));
        return new Response('', { status: 200, headers: { 'content-type': 'text/event-stream' } });
      }),
    );

    for await (const _ of streamChat({
      sessionId: SESSION_ID,
      model: 'm',
      reasoningEffort: null,
      messages: [{ role: 'user', content: 'hi' }],
      signal: new AbortController().signal,
    })) {
      void _;
    }

    // 空对象也必须省掉：服务端会把「有 approvals」当成续跑信号
    expect('approvals' in JSON.parse(bodies[0])).toBe(false);
  });
});

describe('审批事件的 store 处理', () => {
  it('applyEvent 把 approval_required 落到 run.approval，且不伪造工具调用记录', async () => {
    const { createRun, applyEvent } = await import('./chat-store');

    const run = createRun({ sessionId: 's-approval', assistantMsgId: 'a1', model: null });
    expect(run).not.toBeNull();

    applyEvent('s-approval', {
      type: 'approval_required',
      tool_calls: [
        {
          tool_call_id: 'c1',
          tool_name: 'delete_document',
          args: '{"id": 42}',
          description: '删除一篇文档',
          risk_level: 'dangerous',
        },
      ],
    });

    const state = run as RunState;
    expect(state.approval?.toolCalls[0].toolCallId).toBe('c1');
    // 那些调用**没有执行**：混进执行轨迹会让用户以为它们跑过了
    expect(state.toolCalls).toHaveLength(0);
  });
});
